"""Minimal emergency snapshot on an exit. All readers must use load().

The entry delivers images over the existing management SSH session: root on
the exit runs the receiver as vpn2-emergency, so the state stays owned by the
agent user. The installation ID comes from the local agent configuration,
never from a request. Requests and responses carry no secrets in logs.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time


MAX_BYTES = 4 * 1024 * 1024
NAME = 'snapshot.json'
CONFIRMED = 'snapshot-confirmed.json'
STATUSES = ('accepted', 'unchanged', 'stale', 'conflict', 'wrong-installation', 'invalid')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('invalid timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp needs timezone')
    return parsed.timestamp()


def iso_time(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _integer(value):
    return type(value) is int


def _nullable_id(value):
    return value is None or _integer(value)


def _fields(value, names, error):
    # The contract allows new fields; only the known ones are checked.
    if not isinstance(value, dict) or not names <= set(value):
        raise ValueError(error)


def validate(value):
    """Reject ambiguous identities; unknown or malformed grants deny access."""
    _fields(value, {'schema', 'version', 'installation_id', 'created_at', 'exits',
                    'admin_chat_id', 'bot_token', 'clients'}, 'invalid snapshot')
    if not _integer(value['schema']) or value['schema'] != 1:
        raise ValueError('unsupported schema')
    if not _integer(value['version']) or value['version'] < 1:
        raise ValueError('invalid version')
    if not isinstance(value['installation_id'], str) or not value['installation_id']:
        raise ValueError('invalid installation')
    timestamp(value['created_at'])
    if not _nullable_id(value['admin_chat_id']):
        raise ValueError('invalid admin')
    if value['bot_token'] is not None and not isinstance(value['bot_token'], str):
        raise ValueError('invalid bot token')
    if not isinstance(value['exits'], list) or not isinstance(value['clients'], list):
        raise ValueError('invalid lists')
    exits, clients, devices = set(), set(), set()
    for node in value['exits']:
        _fields(node, {'id', 'name', 'ip', 'priority'}, 'invalid exit')
        if not all(isinstance(node[k], str) and node[k] for k in ('id', 'name', 'ip')):
            raise ValueError('invalid exit identity')
        if node['id'] in exits or not _integer(node['priority']):
            raise ValueError('ambiguous exit')
        exits.add(node['id'])
    for client in value['clients']:
        _fields(client, {'id', 'name', 'telegram_chat_id', 'allowed', 'expires_at', 'devices'},
                'invalid client')
        if not _integer(client['id']) or client['id'] in clients:
            raise ValueError('ambiguous client')
        clients.add(client['id'])
        if not isinstance(client['name'], str) or not _nullable_id(client['telegram_chat_id']):
            raise ValueError('invalid client identity')
        # Only a literal true grants access. Unknown/malformed deadlines deny.
        client['allowed'] = client['allowed'] is True
        if client['expires_at'] is not None:
            try:
                timestamp(client['expires_at'])
            except (ValueError, TypeError, OverflowError):
                client['allowed'] = False
                client['expires_at'] = iso_time(0)
        if not isinstance(client['devices'], list):
            raise ValueError('invalid devices')
        for device in client['devices']:
            _fields(device, {'id', 'name'}, 'invalid device')
            if (not _integer(device['id']) or device['id'] in devices
                    or not isinstance(device['name'], str)):
                raise ValueError('ambiguous device')
            devices.add(device['id'])
    return value


def read_private(path):
    """No symlinks, no group/world readers; errors contain no file content."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        stat = os.fstat(stream.fileno())
        if stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
            raise ValueError('insecure state permissions')
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('state too large')
    return json.loads(raw)


def atomic_write(path, value):
    raw = encode(value)
    if len(raw) > MAX_BYTES:
        raise ValueError('state too large')
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.snapshot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def locked(state_dir, name='snapshot.lock'):
    directory = Path(state_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = directory.lstat()
    if directory.is_symlink() or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
        raise ValueError('insecure state directory')
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _effective(value, now):
    for client in value['clients']:
        if client['expires_at'] is not None and timestamp(client['expires_at']) <= now:
            client['allowed'] = False
    return value


def _current(state_dir):
    try:
        return validate(read_private(Path(state_dir) / NAME))
    except FileNotFoundError:
        return None


def _confirm(state_dir, value, now):
    atomic_write(Path(state_dir) / CONFIRMED, {'digest': digest(value), 'at': now})


def _view(value, now):
    """What the entry needs to continue after a conflict: no names, no secrets."""
    if value is None:
        return {'version': None, 'digest': None, 'allowed': None}
    allowed = [c['id'] for c in _effective(json.loads(encode(value)), now)['clients'] if c['allowed']]
    return {'version': value['version'], 'digest': digest(value), 'allowed': allowed}


def accept(state_dir, value, expected_digest, installation_id, now=None):
    """Compare-and-swap: the entry must name the image it replaces.

    A higher version alone is not enough. The entry learns the current image
    from a conflict reply and decides which grants must stay denied before it
    sends a successor; a lost acknowledgement is safe to retry verbatim.
    """
    now = time.time() if now is None else now
    try:
        value = validate(json.loads(encode(value)))
        if value['installation_id'] != installation_id:
            return {'status': 'wrong-installation'}
        with locked(state_dir):
            previous = _current(state_dir)
            if previous is not None:
                if previous['installation_id'] != installation_id:
                    return {'status': 'wrong-installation'}
                if digest(value) == digest(previous):
                    _confirm(state_dir, previous, now)
                    return {'status': 'unchanged'}
                if value['version'] < previous['version']:
                    return {'status': 'stale', **_view(previous, now)}
                if value['version'] == previous['version'] or expected_digest != digest(previous):
                    return {'status': 'conflict', **_view(previous, now)}
            atomic_write(Path(state_dir) / NAME, value)
            _confirm(state_dir, value, now)
        return {'status': 'accepted'}
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return {'status': 'invalid'}


def confirm(state_dir, version, value_digest, installation_id, now=None):
    """Periodic reconciliation without resending the image."""
    now = time.time() if now is None else now
    try:
        with locked(state_dir):
            previous = _current(state_dir)
            if previous is not None and previous['installation_id'] != installation_id:
                return {'status': 'wrong-installation'}
            if previous is None or previous['version'] != version or digest(previous) != value_digest:
                return {'status': 'conflict', **_view(previous, now)}
            _confirm(state_dir, previous, now)
        return {'status': 'unchanged'}
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return {'status': 'invalid'}


def load(state_dir):
    """Effective snapshot: expiry is applied now, also during an outage.

    confirmed_at is added: the last time the entry confirmed this image.
    """
    try:
        with locked(state_dir):
            value = validate(read_private(Path(state_dir) / NAME))
            try:
                confirmed = read_private(Path(state_dir) / CONFIRMED)
            except FileNotFoundError:
                confirmed = {}
        now = time.time()
        seconds = confirmed.get('at') if confirmed.get('digest') == digest(value) else None
        if type(seconds) not in (int, float):
            seconds = timestamp(value['created_at'])
        value['confirmed_at'] = iso_time(min(seconds, now))
        return _effective(value, now)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, AttributeError):
        return None


def age_seconds(state_dir):
    """Seconds since the entry last confirmed the current image, or None."""
    value = load(state_dir)
    return max(0.0, time.time() - timestamp(value['confirmed_at'])) if value else None


def receive(stream, state_dir, installation_id, now=None):
    """One request from the entry. Unknown input yields only 'invalid'."""
    try:
        raw = stream.read(MAX_BYTES + 4097)
        if len(raw) > MAX_BYTES + 4096:
            raise ValueError('request too large')
        request = json.loads(raw)
        if request.get('op') == 'put':
            expected = request['expected_digest']
            if expected is not None and not isinstance(expected, str):
                raise ValueError('invalid digest')
            return accept(state_dir, request['snapshot'], expected, installation_id, now)
        if request.get('op') == 'confirm':
            if not _integer(request['version']) or not isinstance(request['digest'], str):
                raise ValueError('invalid confirmation')
            return confirm(state_dir, request['version'], request['digest'], installation_id, now)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        pass
    return {'status': 'invalid'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('receive',))
    parser.add_argument('--config', default='/etc/vpn2/emergency/agent.json')
    parser.add_argument('--state-dir', default='/var/lib/vpn2-emergency')
    args = parser.parse_args()
    try:
        installation_id = json.loads(Path(args.config).read_text())['installation_id']
        if not isinstance(installation_id, str) or not installation_id:
            raise ValueError('invalid installation')
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'status': 'invalid'}), flush=True)
        return 1
    response = receive(sys.stdin.buffer, args.state_dir, installation_id)
    sys.stdout.write(encode(response).decode() + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
