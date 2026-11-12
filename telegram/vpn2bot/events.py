"""The bot's HTTP server on 127.0.0.1: panel events (POST /event) and GET /health."""
import asyncio
import base64
import hmac
import re
import sqlite3
import time
from contextlib import suppress

from aiohttp import web

from . import api, config, texts
from .config import log
from .outbox import Outbox, QueueFull
from .texts import md, md_bold

OUTBOX_KEY = web.AppKey('outbox', Outbox)


async def report_rejected(payload: dict, status: int) -> None:
    recipient = 'admin' if payload.get('chat_id') == config.CHAT_ID else 'client'
    r = await api.panel_http.post('/api/bot/events', json={
        'type': 'telegram_rejected', 'status': status, 'recipient': recipient})
    r.raise_for_status()


async def event_outbox(webapp):
    webapp[OUTBOX_KEY] = queue = Outbox(config.OUTBOX_PATH)
    worker = asyncio.create_task(queue.run(api.send, report_rejected))
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


async def send_config(data: dict) -> None:
    """WireGuard: the QR picture first, then the file. OpenVPN embeds certificates, so a file only."""
    wireguard = data['type'] == 'client_send_wireguard'
    name, conf = data.get('name', '?'), data.get('conf', '')
    if not conf:
        return
    caption = f'{md_bold(name)} — {"WireGuard" if wireguard else "OpenVPN"}'
    qr = b''
    with suppress(ValueError, TypeError):
        qr = base64.b64decode(data.get('qr'), validate=True)
    if wireguard and qr.startswith(b'\x89PNG\r\n\x1a\n'):
        await api.send_file(config.CHAT_ID, 'photo', 'qr.png', qr, caption)
    await api.send_file(config.CHAT_ID, 'document', f'{name}.{"conf" if wireguard else "ovpn"}',
                        conf.encode('utf-8'), caption)


async def send_ikev2(data: dict) -> None:
    username = data.get('username', '?')
    await api.send(config.CHAT_ID, f'Сервер: `{data.get("server", "?")}`\nЛогин: `{username}`\n'
                   f'Пароль: `{data.get("password", "?")}`\n\n{md_bold(username)} — IKEv2', 'Markdown')
    if data.get('caCertPem'):
        await api.send_file(config.CHAT_ID, 'document', f'{username}-ca.crt',
                            data['caCertPem'].encode('utf-8'), f'{md_bold(username)} — CA-сертификат IKEv2')


async def send_invite(data: dict) -> None:
    if data.get('password', ''):
        await api.send(config.CHAT_ID, f'📨 Приглашение для «{data.get("name", "?")}». Перешлите клиенту сообщение ниже ↓')
        await api.send(config.CHAT_ID, texts.invite(data['password']))


async def announce_deploy(data: dict) -> None:
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
    await api.send(config.CHAT_ID, text, 'Markdown')


# Sent at once past the queue: the admin forwards these to the client himself.
SENT = {
    'client_send_wireguard': send_config,
    'client_send_openvpn': send_config,
    'client_send_ikev2': send_ikev2,
    'client_send_password': send_invite,
    'deploy_done': announce_deploy,
}


async def handle_event(request: web.Request) -> web.Response:
    if not hmac.compare_digest(request.headers.get('X-Tgbot-Secret', '').encode(), config.SECRET.encode()):
        return web.Response(status=403)
    try:
        data = await request.json()
        kind = data.get('type')
        payloads = QUEUED[kind](data) if kind in QUEUED else None
    except Exception:
        return web.Response(status=400)
    if kind in SENT:
        asyncio.create_task(SENT[kind](data))
    if payloads is None:
        return web.Response(text='ok')
    delivery_id = data.get('deliveryId')
    try:
        event_id = request.app[OUTBOX_KEY].enqueue(payloads, delivery_id)
    except ValueError:
        return web.Response(status=400, text='invalid notification')
    except (QueueFull, sqlite3.Error, OSError):
        log.error('outbox cannot accept notification')
        return web.Response(status=503, text='notification queue unavailable')
    return web.json_response({'ok': True, 'event_id': event_id, 'delivery_id': delivery_id})
