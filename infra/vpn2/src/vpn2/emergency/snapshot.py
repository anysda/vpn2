"""Снимок клиентов на экзите: один файл, который entry кладёт по служебному ssh.

Читать только через load(): он проверяет образ и применяет сроки доступа
на текущий момент, в том числе во время аварии.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import time

MAX_BYTES = 4 * 1024 * 1024
NAME = 'snapshot.json'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


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


def _require(ok, error):
    if not ok:
        raise ValueError(error)


def _record(value, names, seen, error):
    """Словарь с известными полями (новые допустимы) и неповторённым id."""
    _require(isinstance(value, dict) and names <= set(value) and value['id'] not in seen, error)
    seen.add(value['id'])


def validate(value):
    """Двусмысленные личности отвергаются; непонятное право - отказ в доступе."""
    _require(isinstance(value, dict) and {'schema', 'version', 'installation_id', 'created_at', 'exits',
                                          'admin_chat_id', 'bot_token', 'clients'} <= set(value), 'invalid snapshot')
    _require(value['schema'] == 1 and _integer(value['version']) and value['version'] >= 1, 'unsupported snapshot')
    _require(isinstance(value['installation_id'], str) and value['installation_id'], 'invalid installation')
    timestamp(value['created_at'])
    _require(value['admin_chat_id'] is None or _integer(value['admin_chat_id']), 'invalid admin')
    _require(value['bot_token'] is None or isinstance(value['bot_token'], str), 'invalid bot token')
    _require(isinstance(value['exits'], list) and isinstance(value['clients'], list), 'invalid lists')
    exits, clients, devices = set(), set(), set()
    for node in value['exits']:
        _record(node, {'id', 'name', 'ip', 'priority'}, exits, 'invalid exit')
        _require(all(isinstance(node[k], str) and node[k] for k in ('id', 'name', 'ip'))
                 and _integer(node['priority']), 'invalid exit')
    for client in value['clients']:
        _record(client, {'id', 'name', 'telegram_chat_id', 'allowed', 'expires_at', 'devices'}, clients,
                'invalid client')
        chat = client['telegram_chat_id']
        _require(_integer(client['id']) and isinstance(client['name'], str) and (chat is None or _integer(chat))
                 and isinstance(client['devices'], list), 'invalid client')
        # Доступ даёт только буквальное true; непонятный срок - отказ.
        client['allowed'] = client['allowed'] is True
        if client['expires_at'] is not None:
            try:
                timestamp(client['expires_at'])
            except (ValueError, TypeError, OverflowError):
                client['allowed'], client['expires_at'] = False, iso_time(0)
        for device in client['devices']:
            _record(device, {'id', 'name'}, devices, 'invalid device')
            _require(_integer(device['id']) and isinstance(device['name'], str), 'invalid device')
    return value


def exit_name(image, exit_id):
    return next((node['name'] for node in (image or {}).get('exits', []) if node['id'] == exit_id), exit_id)


def load(state_dir, now=None):
    """Действующий снимок или None; истёкший срок снимает доступ сразу."""
    now = time.time() if now is None else now
    try:
        with (Path(state_dir) / NAME).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        value = validate(json.loads(raw))
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None
    for client in value['clients']:
        if client['expires_at'] is not None and timestamp(client['expires_at']) <= now:
            client['allowed'] = False
    return value


def age_seconds(state_dir):
    """Сколько секунд назад entry последний раз положил снимок; None - снимка нет."""
    if load(state_dir) is None:
        return None
    return max(0.0, time.time() - (Path(state_dir) / NAME).stat().st_mtime)
