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


def _fields(value, names, error):
    # Новые поля допустимы, проверяются только известные.
    if not isinstance(value, dict) or not names <= set(value):
        raise ValueError(error)


def validate(value):
    """Двусмысленные личности отвергаются; непонятное право - отказ в доступе."""
    _fields(value, {'schema', 'version', 'installation_id', 'created_at', 'exits',
                    'admin_chat_id', 'bot_token', 'clients'}, 'invalid snapshot')
    if value['schema'] != 1 or not _integer(value['version']) or value['version'] < 1:
        raise ValueError('unsupported snapshot')
    if not isinstance(value['installation_id'], str) or not value['installation_id']:
        raise ValueError('invalid installation')
    timestamp(value['created_at'])
    if value['admin_chat_id'] is not None and not _integer(value['admin_chat_id']):
        raise ValueError('invalid admin')
    if value['bot_token'] is not None and not isinstance(value['bot_token'], str):
        raise ValueError('invalid bot token')
    if not isinstance(value['exits'], list) or not isinstance(value['clients'], list):
        raise ValueError('invalid lists')
    exits, clients, devices = set(), set(), set()
    for node in value['exits']:
        _fields(node, {'id', 'name', 'ip', 'priority'}, 'invalid exit')
        if (not all(isinstance(node[k], str) and node[k] for k in ('id', 'name', 'ip'))
                or node['id'] in exits or not _integer(node['priority'])):
            raise ValueError('invalid exit')
        exits.add(node['id'])
    for client in value['clients']:
        _fields(client, {'id', 'name', 'telegram_chat_id', 'allowed', 'expires_at', 'devices'},
                'invalid client')
        chat = client['telegram_chat_id']
        if (not _integer(client['id']) or client['id'] in clients
                or not isinstance(client['name'], str) or not (chat is None or _integer(chat))
                or not isinstance(client['devices'], list)):
            raise ValueError('invalid client')
        clients.add(client['id'])
        # Доступ даёт только буквальное true; непонятный срок - отказ.
        client['allowed'] = client['allowed'] is True
        if client['expires_at'] is not None:
            try:
                timestamp(client['expires_at'])
            except (ValueError, TypeError, OverflowError):
                client['allowed'], client['expires_at'] = False, iso_time(0)
        for device in client['devices']:
            _fields(device, {'id', 'name'}, 'invalid device')
            if (not _integer(device['id']) or device['id'] in devices
                    or not isinstance(device['name'], str)):
                raise ValueError('invalid device')
            devices.add(device['id'])
    return value


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
