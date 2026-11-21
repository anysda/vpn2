"""The bot's HTTP server on 127.0.0.1: panel events (POST /event) and GET /health."""
import asyncio
import base64
import hmac
import re
import sqlite3
import time
from contextlib import suppress

from aiohttp import web

from . import api, config, outbox, texts
from .config import log
from .outbox import Outbox, QueueFull
from .texts import md, md_bold

OUTBOX_KEY = web.AppKey('outbox', Outbox)
# The event loop holds tasks weakly: a deploy notice without a reference here may be collected half-way.
sending: set[asyncio.Task] = set()


async def report_rejected(payload: dict, status: int, delivery_id: str | None = None) -> None:
    recipient = 'admin' if payload.get('chat_id') == config.CHAT_ID else 'client'
    r = await api.panel_http.post('/api/bot/events', json={
        'type': 'telegram_rejected', 'status': status, 'recipient': recipient,
        **({'deliveryId': delivery_id} if delivery_id else {})})
    r.raise_for_status()


async def event_outbox(webapp):
    webapp[OUTBOX_KEY] = queue = Outbox(config.OUTBOX_PATH)
    worker = asyncio.create_task(queue.run(deliver, report_rejected))
    yield
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker


def health(**waiting):
    """GET /health: waiting for a token is healthy; polling is 503 once getUpdates is silent too long."""
    async def handle(_request: web.Request) -> web.Response:
        if waiting:
            return web.json_response({'ok': True, **waiting, 'username': ''})
        idle = round(time.monotonic() - api.LAST_POLL_OK, 1)
        stalled = config.POLL_STALL_SEC > 0 and idle > config.POLL_STALL_SEC
        return web.json_response(
            {'ok': not stalled, 'poll_idle_sec': idle, 'poll_stall_sec': config.POLL_STALL_SEC,
             'username': config.BOT_USERNAME or ''},
            status=503 if stalled else 200)
    return handle


handle_health = health()


def chat_ids(values) -> list[int]:
    if not isinstance(values, list) or not values:
        raise ValueError('no recipients')
    for value in values:
        if isinstance(value, bool) or not re.fullmatch(r'-?[0-9]{1,16}', str(value)) or not int(value):
            raise ValueError('bad chat id')
    return list(dict.fromkeys(map(int, values)))


def admin_note(text: str) -> list[dict]:
    return [{'chat_id': config.CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}]


def system_alert(data: dict) -> list[dict]:
    subject, detail = data.get('subject', 'system alert'), data.get('detail', '')
    if not isinstance(subject, str) or not isinstance(detail, str):
        raise ValueError('bad alert')
    return admin_note(f'⚠️ {md_bold(subject)}' + (f'\n\n{md(detail)}' if detail else ''))


# Queued notifications; the panel writes the client texts, the bot only delivers them.
QUEUED = {
    'client_created': lambda d: admin_note(f'👤 Новый клиент: {md_bold(d.get("name", "?"))}'),
    'broadcast': lambda d: [{'chat_id': chat_id, 'text': d.get('text', '')}
                            for chat_id in chat_ids(d.get('chatIds'))],
    'client_notify': lambda d: [{'chat_id': chat_ids([d.get('chatId')])[0],
                                 'text': d.get('text', '')}],
    'system_alert': system_alert,
}


def admin_file(kind: str, filename: str, content: bytes, caption: str) -> dict:
    return {'chat_id': config.CHAT_ID, 'caption': caption,
            'file': {'kind': kind, 'name': filename, 'data': base64.b64encode(content).decode()}}


async def deliver(chat_id: int, file: dict | None = None, caption: str = '', **message) -> None:
    """One queued message: a text, or a file (photo or document) with its caption."""
    if file:
        await api.send_file(chat_id, file['kind'], file['name'], base64.b64decode(file['data']), caption)
    else:
        await api.send(chat_id, **message)


def config_files(data: dict) -> list[dict]:
    """WireGuard: the QR picture first, then the file. OpenVPN embeds certificates, so a file only."""
    wireguard = data['type'] == 'client_send_wireguard'
    name, conf = data.get('name', '?'), data.get('conf', '')
    if not conf:
        return []
    caption = f'{md_bold(name)} — {"WireGuard" if wireguard else "OpenVPN"}'
    qr = b''
    with suppress(ValueError, TypeError):
        qr = base64.b64decode(data.get('qr'), validate=True)
    photo = [admin_file('photo', 'qr.png', qr, caption)] if wireguard and qr.startswith(b'\x89PNG\r\n\x1a\n') else []
    return photo + [admin_file('document', f'{name}.{"conf" if wireguard else "ovpn"}', conf.encode('utf-8'), caption)]


def ikev2_messages(data: dict) -> list[dict]:
    username = data.get('username', '?')
    text = (f'Сервер: `{data.get("server", "?")}`\nЛогин: `{username}`\n'
            f'Пароль: `{data.get("password", "?")}`\n\n{md_bold(username)} — IKEv2')
    ca = data.get('caCertPem')
    return admin_note(text) + ([admin_file('document', f'{username}-ca.crt', ca.encode('utf-8'),
                                           f'{md_bold(username)} — CA-сертификат IKEv2')] if ca else [])


def invite_messages(data: dict) -> list[dict]:
    if not data.get('password', ''):
        return []
    return [{'chat_id': config.CHAT_ID, 'text': f'📨 Приглашение для «{data.get("name", "?")}». Перешлите клиенту сообщение ниже ↓'},
            {'chat_id': config.CHAT_ID, 'text': texts.invite(data['password'])}]


async def announce_deploy(data: dict, queue: Outbox) -> None:
    unmeasured = data.get('verify_unmeasured') is True
    text = title = '⚠️ *Установка завершена с предупреждениями*' if unmeasured else '✅ *Установка завершена*'
    # Right after a deploy the panel may still be starting.
    for attempt in range(5):
        try:
            text = texts.status(await api.status(), title)
            break
        except Exception as e:
            log.warning('announce_deploy: status failed (try %d): %s', attempt + 1, e)
            await asyncio.sleep(3)
    if unmeasured:
        text += '\n\n? Не проверено: часть проверок не измерена. Причины — в сводке деплоя 99-verify.'
    try:
        queue.enqueue(admin_note(text))
    except Exception as e:
        log.error('outbox cannot accept the deploy notice: %s', type(e).__name__)


# The admin forwards these to the client himself: the panel waits for Telegram's answer, and a message
# that did not leave stays queued for an hour, not for a day.
FORWARDED = {
    'client_send_wireguard': config_files,
    'client_send_openvpn': config_files,
    'client_send_ikev2': ikev2_messages,
    'client_send_password': invite_messages,
}
FORWARD_TTL_SEC = 60 * 60
SEND_WAIT_SEC = 5.0


async def handle_event(request: web.Request) -> web.Response:
    if not hmac.compare_digest(request.headers.get('X-Tgbot-Secret', '').encode(), config.SECRET.encode()):
        return web.Response(status=403)
    try:
        data = await request.json()
        kind = data.get('type')
        build = QUEUED.get(kind) or FORWARDED.get(kind)
        payloads = build(data) if build else None
    except Exception:
        return web.Response(status=400)
    queue = request.app[OUTBOX_KEY]
    if kind == 'deploy_done':
        # deploy.sh does not wait: the summary is queued once the panel answers the status request.
        task = asyncio.create_task(announce_deploy(data, queue))
        sending.add(task)
        task.add_done_callback(sending.discard)
    if payloads is None:
        return web.Response(text='ok')
    delivery_id = data.get('deliveryId')
    try:
        ids = queue.enqueue(payloads, delivery_id, FORWARD_TTL_SEC if kind in FORWARDED else outbox.TTL_SEC)
    except ValueError:
        return web.Response(status=400, text='invalid notification')
    except (QueueFull, sqlite3.Error, OSError):
        log.error('outbox cannot accept notification')
        return web.Response(status=503, text='notification queue unavailable')
    if kind not in FORWARDED:
        return web.json_response({'ok': True, 'event_id': ids[-1], 'delivery_id': delivery_id})
    outcome = await queue.outcome(ids, SEND_WAIT_SEC)
    if outcome is None:
        return web.json_response({'ok': True, 'sent': False}, status=202)
    status = next((status for status in outcome if status), None)
    if status:
        return web.json_response({'ok': False, 'status': status}, status=502)
    return web.json_response({'ok': True, 'sent': True})
