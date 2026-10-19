"""Durable outgoing Bot API delivery; call run_once from a dedicated worker.

The caller enqueues only after panel/Hysteria2 readiness, supplies the persisted
generation and bundle version, and cancels old work on role/lifecycle changes.
snapshot.load is the sole source of authorization and Telegram destinations.
Each acknowledged part is committed independently. A lost response (or a crash
between the response and commit) can still cause a duplicate at Telegram.
"""

from contextlib import contextmanager
from datetime import datetime
import fcntl
from http.client import HTTPException
import importlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib import error, parse, request
import uuid


INITIAL = ('Основное VPN-подключение временно недоступно. Аварийный конфиг '
           'Hysteria2 для устройства {device_name} — ниже. Как подключиться: '
           '{instructions_url}. Сохраните прежний VPN-профиль — он понадобится '
           'после восстановления')
LEADER_CHANGED = ('Аварийный сервер сменился. Для устройства {device_name} '
                  'используйте новый конфиг Hysteria2 ниже. Предыдущий аварийный '
                  'профиль больше не используйте. Как подключиться: {instructions_url}')
RECOVERED = ('Основное VPN-подключение восстановлено. Отключите аварийное '
             'подключение в используемом приложении и включите прежний профиль '
             'WireGuard, OpenVPN или IKEv2. Аварийный доступ будет отключён в '
             '{deadline}, примерно через 10 минут')
RETURN_CANCELLED = ('Возврат отменён: основное VPN-подключение снова недоступно. '
                    'Аварийный доступ сохраняется — если вы уже переключились, снова '
                    'включите аварийное подключение Hysteria2')
ADMIN = ('Основной VPN недоступен. Аварийная панель запущена на {exit_name}. '
         'Адрес: {panel_url}. Пароль: {panel_password}. Для входа нужен только пароль')
ADMIN_SELF_SIGNED = ('. Сертификат самоподписанный: браузер покажет предупреждение о '
                     'небезопасном подключении. Продолжайте, только если отпечаток '
                     'SHA-256 совпадает: {fingerprint}')
ADMIN_LEADER_CHANGED = 'Аварийный сервер сменился — новые адрес и пароль. '
ADMIN_RECOVERED = ('Основной VPN восстановлен. Аварийный режим будет завершён в {deadline}, '
                   'примерно через 10 минут')
ADMIN_RETURN_CANCELLED = ('Основной VPN снова недоступен. Завершение аварийного режима '
                          'отменено, аварийная панель и доступ продолжают работать')
ADMIN_ENTRY_MOVED = '. Основной сервер вернулся с новым адресом: {address}'
EVENTS = {'initial', 'leader_changed', 'recovered', 'return_cancelled'}
DEADLINE = '{deadline}'


def _snapshot(state_dir):
    return importlib.import_module('.snapshot', __package__).load(state_dir) or {}


def _stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('time zone required')
    return parsed


def _human(value):
    # Absolute time with an explicit zone; the caller chooses the zone.
    parsed = _stamp(value)
    offset = parsed.strftime('%z')
    return f'{parsed:%H:%M %d.%m.%Y} (UTC{offset[:3]}:{offset[3:]})'


def _footer(exit_name, issued_at):
    footer = f'\n\nУзел: {exit_name}. Время выдачи: {_human(issued_at)}'
    if _units(footer) > 512:
        raise ValueError('node metadata too long')
    return footer


def _units(text):
    return len(text.encode('utf-16-le')) // 2


def _texts(text, footer):
    # Count UTF-16 units conservatively; never split a Unicode code point.
    limit = 4096 - _units(footer)
    chunk, size = [], 0
    for char in text:
        width = _units(char)
        if size + width > limit:
            yield {'method': 'sendMessage', 'text': ''.join(chunk) + footer}
            chunk, size = [], 0
        chunk.append(char)
        size += width
    if chunk:
        yield {'method': 'sendMessage', 'text': ''.join(chunk) + footer}


def _url(value):
    url = parse.urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise ValueError('HTTPS public URL required')
    return url


def _reason(snapshot, client_id, now):
    clients = [client for client in snapshot.get('clients', []) if client['id'] == client_id]
    if len(clients) != 1:
        return None, 'неизвестные или конфликтующие права'
    client = clients[0]
    # The snapshot also clears "allowed" for an expired client, so the expiry is
    # checked first; epoch 0 is the snapshot's marker for an unreadable deadline.
    if client.get('expires_at') is not None:
        try:
            expires = _stamp(client['expires_at']).timestamp()
        except (TypeError, ValueError):
            return client, 'неизвестные или конфликтующие права'
        if expires == 0:
            return client, 'неизвестные или конфликтующие права'
        if expires <= now:
            return client, 'срок доступа истёк'
    if client.get('allowed') is not True:
        return client, 'клиент заморожен'
    if not client.get('telegram_chat_id'):
        return client, 'нет канала доставки'
    return client, ''


def _prepare(state_dir):
    directory = Path(state_dir) / 'telegram'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


@contextmanager
def _db(state_dir, audience):
    path = _prepare(state_dir) / f'{audience}.sqlite3'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row  # noqa - consumed by sqlite3
    try:
        db.executescript('''
            PRAGMA secure_delete=ON;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS delivery (
                id INTEGER PRIMARY KEY, emergency TEXT NOT NULL, generation INTEGER NOT NULL,
                client INTEGER NOT NULL, version TEXT NOT NULL, event TEXT NOT NULL,
                chat INTEGER, devices TEXT NOT NULL, state TEXT NOT NULL,
                reason TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                due REAL NOT NULL DEFAULT 0,
                UNIQUE(emergency, generation, client, version, event));
            CREATE TABLE IF NOT EXISTS part (
                delivery INTEGER NOT NULL, position INTEGER NOT NULL,
                payload TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(delivery, position));
        ''')
        yield db
    finally:
        db.close()


def _insert(db, emergency_id, generation, client_id, version, event,
            chat, devices, parts, reason):
    cursor = db.execute('''INSERT OR IGNORE INTO delivery
        (emergency,generation,client,version,event,chat,devices,state,reason)
        VALUES (?,?,?,?,?,?,?,?,?)''',
        (emergency_id, generation, client_id, str(version), event, chat,
         json.dumps(devices), 'blocked' if reason else 'pending', reason))
    if cursor.rowcount:
        db.executemany('INSERT INTO part VALUES (?,?,?,0)',
                       ((cursor.lastrowid, i, json.dumps(part, ensure_ascii=False))
                        for i, part in enumerate(parts)))


def _put(state_dir, audience, *args):
    with _db(state_dir, audience) as db, db:
        _insert(db, *args)
    return delivery_status(state_dir, audience)


def enqueue_admin(state_dir, *, emergency_id, generation, bundle_version, exit_name,
                  issued_at, panel_url=None, panel_password=None, panel_ready=False,
                  event='initial', deadline=None, certificate_fingerprint=None,
                  entry_address=None):
    """Persist a ready panel's password once; re-enqueue never replaces it.

    For recovered/return_cancelled no panel credentials are needed. The recovery
    deadline must include a time zone; without it run_once fills in the deadline
    it is given, so the first delivered message fixes the time for everyone.
    """
    if event not in EVENTS:
        raise ValueError('unknown event')
    footer = _footer(exit_name, issued_at)
    if event in {'initial', 'leader_changed'}:
        if not panel_ready:
            return {'admin': 'панель ещё не готова'}
        _url(panel_url)
        if not panel_password:
            raise ValueError('panel password required')
        text = ADMIN.format(exit_name=exit_name, panel_url=panel_url,
                            panel_password=panel_password)
        if event == 'leader_changed':
            text = ADMIN_LEADER_CHANGED + text
        if certificate_fingerprint:
            text += ADMIN_SELF_SIGNED.format(fingerprint=certificate_fingerprint)
    else:
        text = _notification(event, deadline, admin=True)
        if event == 'recovered' and entry_address:
            text += ADMIN_ENTRY_MOVED.format(address=entry_address)
    snapshot = _snapshot(state_dir)
    chat = snapshot.get('admin_chat_id')
    reason = '' if chat else 'нет канала доставки'
    if not snapshot.get('bot_token'):
        reason = 'автодоставка невозможна: нет токена бота'
    return _put(state_dir, 'admin', emergency_id, generation, 0, bundle_version,
                event, chat, [], list(_texts(text, footer)), reason)


def _notification(event, deadline, admin=False):
    if event == 'recovered':
        # No deadline yet: the placeholder stays until the first delivery.
        return (ADMIN_RECOVERED if admin else RECOVERED).format(
            deadline=_human(deadline) if deadline else DEADLINE)
    return ADMIN_RETURN_CANCELLED if admin else RETURN_CANCELLED


def enqueue_client(state_dir, *, emergency_id, generation, client_id, bundle_version,
                   exit_name, issued_at, exit_url, profiles=None, event='initial',
                   deadline=None, now=None):
    """Queue only the client's snapshot device IDs; profiles is hy2.start's map.

    Normal profiles are sent as intact URI messages. Oversized URIs are UTF-8
    documents, avoiding unusable split credentials. No password/admin payload
    argument exists on the client path. Device names remain plain, unparsed text.
    """
    row = _client_row(_snapshot(state_dir), client_id, emergency_id=emergency_id,
                      generation=generation, bundle_version=bundle_version, exit_name=exit_name,
                      issued_at=issued_at, exit_url=exit_url, profiles=profiles, event=event,
                      deadline=deadline, now=now)
    return _put(state_dir, 'client', *row)


def _client_row(snapshot, client_id, *, emergency_id, generation, bundle_version, exit_name,
                issued_at, exit_url, profiles=None, event='initial', deadline=None, now=None):
    if event not in EVENTS:
        raise ValueError('unknown event')
    url = _url(exit_url)
    instructions = parse.urlunsplit((url.scheme, url.netloc, '/instructions', '', ''))
    footer = _footer(exit_name, issued_at)
    client, reason = _reason(snapshot, client_id, time.time() if now is None else now)
    parts, devices = [], []
    if not reason:
        if event in {'initial', 'leader_changed'}:
            template = INITIAL if event == 'initial' else LEADER_CHANGED
            for device in client['devices']:
                device_id = device['id']
                profile = (profiles or {}).get(device_id)
                if not profile or not profile.get('uri', '').startswith('hysteria2://'):
                    # Never send a partial bundle; the admin sees why.
                    parts, devices, reason = [], [], 'нет аварийного профиля устройства'
                    break
                devices.append(device_id)
                parts.extend(_texts(template.format(device_name=device['name'],
                                                    instructions_url=instructions), footer))
                uri = profile['uri']
                if _units(uri + footer) <= 4096:
                    parts.extend(_texts(uri, footer))
                else:
                    parts.append({'method': 'sendDocument', 'document': uri,
                                  'filename': f'hysteria2-{device_id}.txt',
                                  'caption': footer.strip()})
            if not devices and not reason:
                reason = 'нет устройств'
        else:
            parts.extend(_texts(_notification(event, deadline), footer))
    if not snapshot.get('bot_token') and not reason:
        reason = 'автодоставка невозможна: нет токена бота'
    return (emergency_id, generation, client_id, bundle_version, event,
            client.get('telegram_chat_id') if client else None, devices, parts, reason)


def enqueue_clients(state_dir, *, clients=None, **kwargs):
    """Queue every snapshot client (or only the given IDs), so each denial reason
    is visible to the admin. One snapshot read and one transaction for all."""
    snapshot = _snapshot(state_dir)
    ids = [client['id'] for client in snapshot.get('clients', [])]
    if clients is not None:
        wanted = set(clients)
        ids = [client_id for client_id in ids if client_id in wanted]
    rows = [_client_row(snapshot, client_id, **kwargs) for client_id in dict.fromkeys(ids)]
    with _db(state_dir, 'client') as db, db:
        for row in rows:
            _insert(db, *row)
    return delivery_status(state_dir)


def delivery_status(state_dir, audience='client'):
    """Safe panel projection: no message bodies, passwords, tokens or API errors."""
    if audience not in {'admin', 'client'}:
        raise ValueError('unknown audience')
    with _db(state_dir, audience) as db:
        return [dict(row) for row in db.execute('''SELECT emergency,generation,client,
            version,event,state,reason,attempts,due,
            (SELECT count(*) FROM part WHERE delivery=delivery.id AND sent=1) AS sent,
            (SELECT count(*) FROM part WHERE delivery=delivery.id) AS total
            FROM delivery ORDER BY id''')]


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa - urllib callback
        del req, fp, code, msg, headers, newurl
        return None


def _send(token, chat, part, api_base, timeout, opener=None):
    # Never include raw exceptions/responses in status: both can contain secrets.
    endpoint = parse.urlsplit(api_base)
    if (endpoint.scheme != 'https'
            and not (endpoint.scheme == 'http' and endpoint.hostname in {'127.0.0.1', '::1'})):
        raise ValueError('HTTPS Bot API required')
    method = part['method']
    if method == 'sendMessage':
        body = json.dumps({'chat_id': chat, 'text': part['text'],
                           'link_preview_options': {'is_disabled': True}}).encode()
        content_type = 'application/json'
    else:
        boundary = uuid.uuid4().hex
        fields = {'chat_id': str(chat), 'caption': part['caption']}
        body = b''
        for key, value in fields.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                     f'\r\n\r\n{value}\r\n').encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                 f'filename="{part["filename"]}"\r\nContent-Type: text/plain; '
                 'charset=utf-8\r\n\r\n').encode()
        body += part['document'].encode() + f'\r\n--{boundary}--\r\n'.encode()
        content_type = f'multipart/form-data; boundary={boundary}'
    req = request.Request(f'{api_base.rstrip("/")}/bot{parse.quote(token, safe=":")}/{method}',
                          data=body, headers={'Content-Type': content_type})
    try:
        opener = opener or request.build_opener(_NoRedirect())
        try:
            response = opener.open(req, timeout=timeout)
        except error.HTTPError as exc:
            response = exc
        with response:
            status = response.code
            data = json.loads(response.read(65536))
        if status == 200 and data.get('ok') is True:
            return True, '', 0
        code = data.get('error_code', status)
        if code == 429:
            delay = float(data.get('parameters', {}).get('retry_after', 1))
            if not math.isfinite(delay) or delay < 0:
                delay = 1
            return False, 'Telegram 429', max(1, delay)
        return False, f'Telegram {code}' if isinstance(code, int) else 'ошибка Telegram', 0
    except (OSError, HTTPException, ValueError, TypeError, AttributeError):
        return False, 'Telegram недоступен или ответ не подтверждён', 0


def run_once(state_dir, *, api_base='https://api.telegram.org', timeout=5, now=None,
             max_messages=16, deadline=None):
    """Bounded worker tick, admin first, at most one part per recipient per tick.

    No sleeps or background threads. Schedule ticks outside Hysteria2/web startup.
    A nonblocking process lock prevents concurrent workers sending the same part.
    Retry clocks are persisted wall times so process/host restarts preserve them.
    deadline (ISO with zone) fills recovery messages queued without one; they
    wait while it is None. One opener serves the whole pass: building one per
    message loaded the CA store each time.
    """
    directory = _prepare(state_dir)
    fd = os.open(directory / 'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    sent = 0
    opener = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        for audience in ('admin', 'client'):
            with _db(state_dir, audience) as db:
                rows = db.execute("SELECT * FROM delivery WHERE state IN ('pending','retry','blocked') "
                                  'ORDER BY due,id').fetchall()
                chats = set()
                snapshot = _snapshot(state_dir)
                for row in rows:
                    current = time.time() if now is None else now
                    if row['due'] > current or max_messages <= 0:
                        continue
                    reason = ''
                    if audience == 'client':
                        client, reason = _reason(snapshot, row['client'], current)
                        chat = client.get('telegram_chat_id') if client else None
                        if not reason and not set(json.loads(row['devices'])).issubset(
                                {d['id'] for d in client['devices']}):
                            reason = 'состав устройств изменился'
                    else:
                        chat = snapshot.get('admin_chat_id')
                        if not chat:
                            reason = 'нет канала доставки'
                    if row['chat'] is not None and row['chat'] != chat:
                        reason = 'канал доставки изменился'
                    token = snapshot.get('bot_token')
                    if not reason and not token:
                        reason = 'автодоставка невозможна: нет токена бота'
                    if reason:
                        with db:
                            db.execute("UPDATE delivery SET state='blocked',reason=? WHERE id=?",
                                       (reason, row['id']))
                        continue
                    if chat in chats:
                        continue
                    part = db.execute('SELECT * FROM part WHERE delivery=? AND sent=0 '
                                      'ORDER BY position LIMIT 1', (row['id'],)).fetchone()
                    if part is None:
                        # A blocked enqueue with no profiles must be explicitly reissued.
                        continue
                    payload = json.loads(part['payload'])
                    if row['event'] == 'recovered' and DEADLINE in payload.get('text', ''):
                        if deadline is None:
                            continue
                        payload['text'] = payload['text'].replace(DEADLINE, _human(deadline))
                    chats.add(chat)
                    max_messages -= 1
                    opener = opener or request.build_opener(_NoRedirect())
                    ok, reason, retry_after = _send(token, chat, payload, api_base, timeout, opener)
                    with db:
                        if ok:
                            db.execute("UPDATE part SET sent=1,payload='' WHERE delivery=? AND position=?",
                                       (row['id'], part['position']))
                            remaining = db.execute('SELECT count(*) FROM part WHERE delivery=? AND sent=0',
                                                   (row['id'],)).fetchone()[0]
                            db.execute('UPDATE delivery SET state=?,reason=?,attempts=0,due=0 WHERE id=?',
                                       ('pending' if remaining else 'sent', '', row['id']))
                            sent += 1
                        else:
                            # The return notice fixes a ten-minute deadline when it
                            # arrives; an hour of backoff would delay it as long.
                            cap = 60 if row['event'] in {'recovered', 'return_cancelled'} else 3600
                            delay = retry_after or min(cap, 5 * 2 ** min(row['attempts'], 10))
                            db.execute("UPDATE delivery SET state='retry',reason=?,attempts=attempts+1,"
                                       'due=? WHERE id=?', (reason, current + delay, row['id']))
        return sent
    finally:
        os.close(fd)


def reachable(state_dir, *, api_base='https://api.telegram.org', timeout=5):
    """Whether the Bot API answers at all; sends nothing to anyone."""
    token = _snapshot(state_dir).get('bot_token')
    if not token:
        # Without a token nothing can go out anyway.
        return True
    req = request.Request(f'{api_base.rstrip("/")}/bot{parse.quote(token, safe=":")}/getMe')
    try:
        with request.build_opener(_NoRedirect()).open(req, timeout=timeout):
            return True
    except error.HTTPError as exc:
        exc.close()
        return True
    except (OSError, HTTPException, ValueError):
        return False


def cancel(state_dir, *, emergency_id, generation=None, events=None):
    """Stop pending sends on step-down/end; retain dedup tombstones, erase secrets.

    Recovery calls this for initial/leader_changed. Return cancellation calls it
    for recovered before queuing a new notification version. Call from the same
    worker as run_once (or stop/join that worker before changing lifecycle).
    """
    with _worker_lock(state_dir):
        for audience in ('admin', 'client'):
            with _db(state_dir, audience) as db, db:
                query, args = 'emergency=?', [emergency_id]
                if generation is not None:
                    query += ' AND generation=?'
                    args.append(generation)
                if events is not None:
                    query += f' AND event IN ({",".join("?" for _ in events)})'
                    args.extend(events)
                db.execute(f"UPDATE delivery SET state='cancelled',reason='' WHERE {query} "
                           "AND state!='sent'", args)
                db.execute(f"UPDATE part SET payload='' WHERE delivery IN "
                           f'(SELECT id FROM delivery WHERE {query})', args)


@contextmanager
def _worker_lock(state_dir):
    fd = os.open(_prepare(state_dir) / 'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def prune(state_dir, *, keep_emergency_id):
    """At a new emergency, discard older delivery history after cancelling it.

    The lifecycle owner must never enqueue an ended emergency again. Current
    emergency tombstones are retained for its entire lifetime (no age timeout).
    """
    with _worker_lock(state_dir):
        for audience in ('admin', 'client'):
            with _db(state_dir, audience) as db, db:
                db.execute('DELETE FROM part WHERE delivery IN '
                           '(SELECT id FROM delivery WHERE emergency!=?)', (keep_emergency_id,))
                db.execute('DELETE FROM delivery WHERE emergency!=?', (keep_emergency_id,))
