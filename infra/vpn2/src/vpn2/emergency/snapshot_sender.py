"""Рассылка снимка клиентов с entry: данные панели только на чтение, доставка по служебному ssh.

Отдельная служба entry. Панель root не получает: её данные читает дочерний
процесс под пользователем панели, а эта служба лишь кладёт готовый файл на
каждый экзит ключом entry. В журнале - только счётчики исходов.

Клиент допущен, когда желаемое состояние включено (не удаляется, не заморожен,
срок не истёк), а текущая операция доступа (version == clients.access_version)
его включает и подтверждена службами VPN. Подтверждённая операция на перепроверке
(снова pending, версия та же) остаётся допуском; новая правка, неизвестное или
неподтверждённое - отказ.
"""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from .. import fsutil
from . import snapshot

RECONCILE_SECONDS = 60
POLL_SECONDS = 5
SSH_TIMEOUT = 30
# setpriv, а не runuser: без строк сеанса PAM в журнале на каждый вызов.
REMOTE = ("exec setpriv --reuid=vpn2-emergency --regid=vpn2-emergency --init-groups -- sh -c '"
          'umask 077; f=/var/lib/vpn2-emergency/snapshot.json; cat > "$f.new" && '
          '[ "$(stat -c %s "$f.new")" = {size} ] && mv "$f.new" "$f"\'')


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
        current = {}
        for client_id, version, status, payload in db.execute(
                'SELECT client_id, version, status, payload FROM access_operations'):
            current[(client_id, version)] = status, _enabled(payload)
        for client_id, name, chat_id, frozen, expiry, deleting, version in db.execute(
                'SELECT id, name, tg_chat_id, frozen_manual, expires_at, deleting, access_version '
                'FROM clients ORDER BY id'):
            known_expiry = expiry is None or type(expiry) is int
            try:
                expires_at = snapshot.iso_time(expiry) if expiry is not None else None
            except (ValueError, TypeError, OverflowError, OSError):
                known_expiry, expires_at = False, snapshot.iso_time(0)
            # A new operation supersedes all older ones: only the current one counts.
            status, enabled = current.get((client_id, version), (None, False))
            wanted = (deleting == 0 and frozen == 0 and known_expiry
                      and (expiry is None or expiry > now) and enabled)
            clients.append({
                'id': client_id, 'name': name, 'telegram_chat_id': _chat_id(chat_id),
                'allowed': wanted and status == 'confirmed',
                'recheck': wanted and status == 'pending',
                'expires_at': expires_at, 'devices': devices.get(client_id, []),
                'access_version': version if type(version) is int else -1,
            })
    try:
        telegram = json.loads(Path(telegram_file).read_text())
    except FileNotFoundError:
        telegram = {}
    token = telegram.get('bot_token') if isinstance(telegram, dict) else None
    chat = telegram.get('chat_id') if isinstance(telegram, dict) else None
    return {'clients': clients, 'bot_token': token if isinstance(token, str) and token else None,
            'admin_chat_id': _chat_id(chat)}


def collect_as(user, database, telegram_file, now):
    """Read panel data with the panel user's own rights (WAL files stay its own)."""
    command = [sys.executable, '-B', '-m', 'vpn2.emergency', 'snapshot-sender', 'collect',
               '--database', str(database), '--telegram-file', str(telegram_file),
               '--now', repr(now)]
    if user:
        command = ['setpriv', f'--reuid={user}', f'--regid={user}', '--init-groups', '--',
                   *command]
    result = subprocess.run(command, cwd=Path(__file__).resolve().parents[2], input=b'',
                            capture_output=True, timeout=SSH_TIMEOUT, check=False)
    if result.returncode:
        raise ValueError('panel data unavailable')
    return json.loads(result.stdout)


def exits_from(agent_config):
    """Состав как у агента; приоритет - порядок из конфига."""
    entry = agent_config.get('entry_id', 'entry')
    tags = [tag for tag in agent_config['nodes'] if tag != entry]
    return [{'id': tag, 'name': tag, 'ip': agent_config['nodes'][tag]['host'], 'priority': index}
            for index, tag in enumerate(tags)]


def push(ssh, exit_id, raw):
    """Файл целиком по служебному ssh; обрезанная передача на место не встаёт."""
    remote = REMOTE.format(size=len(raw))
    command = [*ssh, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'UpdateHostKeys=no', '-o', 'ConnectTimeout=10', exit_id, remote]
    try:
        return subprocess.run(command, input=raw, capture_output=True, timeout=SSH_TIMEOUT,
                              check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _keep_granted(granted, clients):
    """Панель перепроверяет подтверждённую операцию и на это время помечает её
    pending с той же версией: доступ, подтверждённый для версии, держится до новой правки."""
    for client in clients:
        key, version = str(client['id']), client.pop('access_version')
        recheck = client.pop('recheck', False)
        if client['allowed']:
            granted[key] = version
        elif recheck and granted.get(key) == version:
            client['allowed'] = True
        else:
            granted.pop(key, None)
    for key in set(granted) - {str(client['id']) for client in clients}:
        del granted[key]


def run_once(config, deliver, collector, now=None):
    """Один проход: новая версия только при изменении, файл каждому экзиту раз в минуту."""
    now = time.time() if now is None else now
    path = Path(config['state_dir']) / 'sender.json'
    installation = config['agent']['installation_id']
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    # Без 'image' - файл отправителя v1.1.5 другой схемы: начать заново, как с чистой установки.
    if state.get('installation_id') != installation or 'image' not in state:
        state = {'installation_id': installation, 'image': None, 'granted': {}, 'sent': {}}
    data = collector(now)
    _keep_granted(state['granted'], data['clients'])
    content = {'schema': 1, 'installation_id': installation, 'exits': exits_from(config['agent']),
               'admin_chat_id': data['admin_chat_id'], 'bot_token': data['bot_token'],
               'clients': data['clients']}
    image = state['image']
    if image is None or {k: v for k, v in image.items() if k not in ('version', 'created_at')} != content:
        image = snapshot.validate(content | {'version': (image['version'] if image else 0) + 1,
                                             'created_at': snapshot.iso_time(now)})
        state['image'], state['sent'] = image, {}
    raw, results = snapshot.encode(image), {}
    for node in content['exits']:
        exit_id = node['id']
        if now - state['sent'].get(exit_id, 0) < RECONCILE_SECONDS:
            results[exit_id] = 'unchanged'
        elif deliver(exit_id, raw):
            state['sent'][exit_id], results[exit_id] = now, 'accepted'
        else:
            results[exit_id] = 'unavailable'
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fsutil.atomic(path, snapshot.encode(state))
    return results


def main(argv=None):
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
    args = parser.parse_args(argv)
    if args.command == 'collect':
        try:
            data = collect(args.database, args.telegram_file, args.now or time.time())
        except (OSError, ValueError, sqlite3.Error):
            return 1
        sys.stdout.write(snapshot.encode(data).decode())
        return 0
    ssh = ['ssh', '-F', args.ssh_config]
    last = None
    while True:
        try:
            config = {'state_dir': args.state_dir,
                      'agent': json.loads(Path(args.agent_config).read_text())}
            results = run_once(config, lambda exit_id, raw: push(ssh, exit_id, raw),
                               lambda now: collect_as(args.panel_user or None, args.database,
                                                      args.telegram_file, now))
            # Только счётчики исходов: ни токенов, ни имён, ни текста ошибок.
            values = list(results.values())
            line = json.dumps({status: values.count(status) for status in sorted(set(values))})
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            results, line = {'source': 'unavailable'}, 'snapshot source unavailable'
        if line != last or 'accepted' in results.values():
            print(line, flush=True)
            last = line
        if args.once:
            return 0 if all(v in ('accepted', 'unchanged') for v in results.values()) else 1
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    raise SystemExit(main())
