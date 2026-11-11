"""Очередь сообщений Telegram: один файл на сообщение в <state>/spool.

Ключ сообщения - защита от повтора: то же событие второй раз не ставится.
Сообщение без канала или права хранится с причиной: её показывает панель.
"""

import hashlib
import json
from pathlib import Path
import time

from .. import fsutil

RETRY_FIRST = 30
RETRY_MAX = 300
OPEN = ('pending', 'retry')


def _home(directory):
    return Path(directory) / 'spool'


def _path(directory, key):
    return _home(directory) / (hashlib.sha256(key.encode()).hexdigest()[:32] + '.json')


def _items(directory):
    items = []
    for path in _home(directory).glob('*.json'):
        item = fsutil.read_json(path, None)
        if isinstance(item, dict) and 'key' in item:
            items.append(item)
    return sorted(items, key=lambda item: (item['created'], item['key']))


def _save(directory, item):
    fsutil.atomic(_path(directory, item['key']), json.dumps(item, ensure_ascii=False))


def put(directory, key, chat, parts, *, reason=None, **meta):
    """Ставит сообщение; reason - отказ без отправки. False - такое уже стоит."""
    _home(directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    if _path(directory, key).exists():
        return False
    _save(directory, dict(meta, key=key, chat=chat, parts=parts, sent=0, results=[],
                          state='blocked' if reason else 'pending', reason=reason or '',
                          after=0, tries=0, created=time.time()))
    return True


def rows(directory, **match):
    """Строки без текста сообщений; match отбирает по полям."""
    return [{k: v for k, v in item.items() if k != 'parts'} for item in _items(directory)
            if all(item.get(k) == v for k, v in match.items())]


def flush(directory, api, now=None):
    """Одна попытка на каждое ждущее сообщение, по порядку внутри чата.

    Сеть или 429 останавливают проход: дальше слать бессмысленно.
    """
    now = time.time() if now is None else now
    held = set()
    for item in _items(directory):
        if item['state'] not in OPEN or item['chat'] in held:
            continue
        if item['after'] > now:
            held.add(item['chat'])
            continue
        while item['sent'] < len(item['parts']):
            reply = api.call(item['chat'], item['parts'][item['sent']])
            if not reply.ok:
                break
            item['sent'] += 1
            item['results'].append((reply.result or {}).get('message_id'))
            _save(directory, item)
        else:
            item.update(state='sent', reason='')
            _save(directory, item)
            continue
        item['tries'] += 1
        delay = max(reply.retry_after, min(RETRY_MAX, RETRY_FIRST * 2 ** (item['tries'] - 1)))
        item.update(state='failed' if reply.final else 'retry', reason=reply.reason, after=now + delay)
        _save(directory, item)
        held.add(item['chat'])
        if not reply.final and (reply.retry_after or reply.reason.startswith('Telegram недоступен')):
            return False
    return True


def cancel(directory, **match):
    """Неотправленное по отбору больше не уйдёт."""
    for item in _items(directory):
        if item['state'] in OPEN and all(item.get(k) == v for k, v in match.items()):
            item['state'] = 'cancelled'
            _save(directory, item)


def clear(directory, keep_emergency=None):
    """Убирает сообщения прошлых аварий."""
    for item in _items(directory):
        if keep_emergency is None or item.get('emergency') != keep_emergency:
            _path(directory, item['key']).unlink(missing_ok=True)
