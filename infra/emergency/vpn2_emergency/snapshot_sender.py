"""Entry snapshot publisher: read-only panel data, delivery over management SSH.

Runs on the entry as a separate system service. The panel never gets root:
panel data is read by a child process running as the panel user, while this
process only uses the entry's existing management SSH key with the pinned
exit host keys. Logs contain fixed status counts only.

Access is taken from the panel access model: a client is allowed only when
its desired state is enabled (not deleting, not frozen, not expired) and the
latest confirmed access operation also left it enabled. Anything unknown or
pending denies.
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from . import snapshot


RECONCILE_SECONDS = 60
POLL_SECONDS = 5
SSH_TIMEOUT = 30
KEEP_PRODUCED = 16
REMOTE = ('cd /opt/vpn2-emergency && exec runuser -u vpn2-emergency -- /usr/bin/python3 -B '
          '-m vpn2_emergency.snapshot receive --config /etc/vpn2/emergency/agent.json '
          '--state-dir /var/lib/vpn2-emergency')


def _chat_id(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.lstrip('-').isdigit():
        return int(value)
    return None


def _enabled(payload):
    try:
        return json.loads(payload).get('enabled') is True
    except (ValueError, TypeError, AttributeError):
        return False


def _read_limited(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('telegram settings too large')
    return json.loads(raw)


def collect(database, telegram_file, now):
    """Explicit column allowlist; one SQLite read transaction, including WAL."""
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    clients = []
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        db.execute('BEGIN')
        devices = {}
        for device_id, client_id, name in db.execute(
                'SELECT id, client_id, name FROM devices WHERE deleting = 0 ORDER BY id'):
            devices.setdefault(client_id, []).append({'id': device_id, 'name': name})
        operations = {}
        for client_id, version, status, payload in db.execute(
                "SELECT client_id, version, status, payload FROM access_operations "
                "WHERE status != 'superseded' ORDER BY client_id, version"):
            item = operations.setdefault(client_id, {'current': {}, 'confirmed': False})
            item['current'][version] = _enabled(payload)
            if status == 'confirmed':
                item['confirmed'] = _enabled(payload)
        for client_id, name, chat_id, frozen, expiry, deleting, version in db.execute(
                'SELECT id, name, tg_chat_id, frozen_manual, expires_at, deleting, access_version '
                'FROM clients ORDER BY id'):
            known_expiry = expiry is None or type(expiry) is int
            try:
                expires_at = snapshot.iso_time(expiry) if expiry is not None else None
            except (ValueError, TypeError, OverflowError, OSError):
                known_expiry, expires_at = False, snapshot.iso_time(0)
            access = operations.get(client_id, {'current': {}, 'confirmed': False})
            desired = (deleting == 0 and frozen == 0 and known_expiry
                       and (expiry is None or expiry > now)
                       and access['current'].get(version) is True)
            clients.append({
                'id': client_id, 'name': name, 'telegram_chat_id': _chat_id(chat_id),
                'allowed': desired and access['confirmed'] is True,
                'expires_at': expires_at, 'devices': devices.get(client_id, []),
                'access_version': version if type(version) is int else -1,
            })
    try:
        telegram = _read_limited(telegram_file)
    except FileNotFoundError:
        telegram = {}
    token = telegram.get('bot_token') if isinstance(telegram, dict) else None
    chat = telegram.get('chat_id') if isinstance(telegram, dict) else None
    return {'clients': clients, 'bot_token': token if isinstance(token, str) and token else None,
            'admin_chat_id': _chat_id(chat)}


def collect_as(user, database, telegram_file, now):
    """Read panel data with the panel user's own rights (WAL files stay its own)."""
    command = [sys.executable, '-B', '-m', 'vpn2_emergency.snapshot_sender', 'collect',
               '--database', str(database), '--telegram-file', str(telegram_file),
               '--now', repr(now)]
    if user:
        command = ['runuser', '-u', user, '--', *command]
    result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], input=b'',
                            capture_output=True, timeout=SSH_TIMEOUT, check=False)
    if result.returncode:
        raise ValueError('panel data unavailable')
    return json.loads(result.stdout)


def exits_from(agent_config):
    """Membership exactly as the agent sees it; priority keeps deploy order."""
    entry = agent_config.get('entry_id', 'entry')
    return [{'id': tag, 'name': tag, 'ip': item['host'], 'priority': index}
            for index, (tag, item) in enumerate(
                (tag, item) for tag, item in agent_config['nodes'].items() if tag != entry)]


def ssh_deliver(ssh, exit_id, request):
    """Existing management session: entry key, pinned host key, request on stdin."""
    command = [*ssh, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'UpdateHostKeys=no', '-o', 'ConnectTimeout=10', exit_id, REMOTE]
    try:
        result = subprocess.run(command, input=snapshot.encode(request), capture_output=True,
                                timeout=SSH_TIMEOUT, check=False)
        response = json.loads(result.stdout[:65536].decode().strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, UnicodeDecodeError):
        return {'status': 'unavailable'}
    if not isinstance(response, dict) or response.get('status') not in snapshot.STATUSES:
        return {'status': 'unavailable'}
    return response


def _content(value):
    return {k: v for k, v in value.items() if k not in ('version', 'created_at')}


def _new_state(installation_id):
    return {'installation_id': installation_id, 'snapshot': None, 'produced': [], 'floor': 0,
            'acks': {}, 'confirmed': {}, 'access': {}, 'holds': {}, 'blocked': []}


def _track_access(state, clients):
    """A lower access version than seen means a restored panel database."""
    for client in clients:
        key, version = str(client['id']), client['access_version']
        held = state['holds'].get(key)
        if held is not None and version != held:
            del state['holds'][key]
        seen = state['access'].get(key)
        if seen is not None and version < seen:
            state['holds'][key] = version
            state['access'][key] = version
        else:
            state['access'][key] = max(version, seen if seen is not None else version)
    present = {str(client['id']) for client in clients}
    for key in set(state['holds']) - present:
        del state['holds'][key]


def _hold_widening(state, clients, exit_allowed):
    """Grants the exit does not have stay denied until the next panel edit."""
    allowed = set(exit_allowed)
    for client in clients:
        if client['allowed'] and client['id'] not in allowed:
            state['holds'][str(client['id'])] = client['access_version']


def _image(state, data, exits, installation_id, now):
    clients = [{k: v for k, v in client.items() if k != 'access_version'}
               | {'allowed': client['allowed'] and str(client['id']) not in state['holds']}
               for client in data['clients']]
    content = {'schema': 1, 'installation_id': installation_id, 'exits': exits,
               'admin_chat_id': data['admin_chat_id'], 'bot_token': data['bot_token'],
               'clients': clients}
    previous = state['snapshot']
    if (previous is not None and _content(previous) == content
            and previous['version'] > state['floor']):
        return False
    version = max(previous['version'] if previous else 0, state['floor']) + 1
    state['snapshot'] = snapshot.validate(content | {'version': version,
                                                     'created_at': snapshot.iso_time(now)})
    state['produced'] = (state['produced'] + [snapshot.digest(state['snapshot'])])[-KEEP_PRODUCED:]
    return True


def run_once(config, deliver, collector, now=None):
    """One pass: new version only on change, confirmation every minute per exit."""
    now = time.time() if now is None else now
    state_dir = config['state_dir']
    installation_id = config['agent']['installation_id']
    exits = exits_from(config['agent'])
    with snapshot.locked(state_dir, 'sender.lock'):
        path = Path(state_dir) / 'sender.json'
        try:
            state = snapshot.read_private(path)
        except FileNotFoundError:
            state = None
        if state is None or state.get('installation_id') != installation_id:
            state = _new_state(installation_id)
        data = collector(now)
        _track_access(state, data['clients'])
        results = {}
        for _round in range(3):
            if _image(state, data, exits, installation_id, now):
                # Persist before the first send: a retry after a crash is identical.
                snapshot.atomic_write(path, state)
            current = state['snapshot']
            current_digest = snapshot.digest(current)
            again = False
            for node in exits:
                exit_id = node['id']
                if exit_id in state['blocked']:
                    results[exit_id] = 'wrong-installation'
                    continue
                if state['acks'].get(exit_id) == current_digest:
                    if now - state['confirmed'].get(exit_id, 0) < config.get(
                            'reconcile_seconds', RECONCILE_SECONDS):
                        results[exit_id] = 'unchanged'
                        continue
                    request = {'op': 'confirm', 'version': current['version'],
                               'digest': current_digest}
                else:
                    request = {'op': 'put', 'snapshot': current,
                               'expected_digest': state['acks'].get(exit_id)}
                response = deliver(exit_id, request)
                status = response.get('status')
                results[exit_id] = status
                if status in ('accepted', 'unchanged'):
                    state['acks'][exit_id] = current_digest
                    state['confirmed'][exit_id] = now
                elif status in ('stale', 'conflict'):
                    seen = response.get('digest')
                    version = response.get('version')
                    if seen is not None and seen not in state['produced']:
                        # Unknown newer data on the exit, e.g. a restored entry.
                        _hold_widening(state, data['clients'], response.get('allowed') or [])
                    if type(version) is int:
                        state['floor'] = max(state['floor'], version)
                    state['acks'][exit_id] = seen
                    again = True
                elif status == 'wrong-installation':
                    state['blocked'].append(exit_id)
                snapshot.atomic_write(path, state)
            if not again:
                break
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=('run', 'collect'), default='run')
    parser.add_argument('--agent-config', default='/etc/vpn2/emergency/agent.json')
    parser.add_argument('--state-dir', default='/var/lib/vpn2-emergency-sender')
    parser.add_argument('--database', default='/var/lib/anysda-vpn2/db.sqlite')
    parser.add_argument('--telegram-file',
                        default='/var/lib/anysda-vpn2/runtime/telegram-runtime.json')
    parser.add_argument('--panel-user', default='vpn2-panel')
    parser.add_argument('--ssh-config', default='/root/.ssh/anysda-mgmt.conf')
    parser.add_argument('--now', type=float)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.command == 'collect':
        try:
            data = collect(args.database, args.telegram_file, args.now or time.time())
        except (OSError, ValueError, sqlite3.Error):
            return 1
        sys.stdout.write(snapshot.encode(data).decode())
        return 0
    ssh = ['ssh', '-F', args.ssh_config]
    while True:
        results = {}
        try:
            config = {'state_dir': args.state_dir,
                      'agent': json.loads(Path(args.agent_config).read_text())}
            results = run_once(
                config, lambda exit_id, request: ssh_deliver(ssh, exit_id, request),
                lambda now: collect_as(args.panel_user or None, args.database,
                                       args.telegram_file, now))
            # Fixed vocabulary only: no tokens, names or exception text.
            values = list(results.values())
            print(json.dumps({status: values.count(status) for status in sorted(set(values))}),
                  flush=True)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            print('snapshot source unavailable', flush=True)
            results = {'source': 'unavailable'}
        if args.once:
            return 0 if all(v in ('accepted', 'unchanged') for v in results.values()) else 1
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    raise SystemExit(main())
