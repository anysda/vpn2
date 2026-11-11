"""HTTP-сервер бота на 127.0.0.1: события панели (POST /event) и GET /health."""
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
    """Отказ Telegram — тревога в журнал панели. В Telegram бот об этом не пишет."""
    recipient = 'admin' if payload.get('chat_id') == config.CHAT_ID else 'client'
    r = await api.panel_http.post('/api/bot/events', json={
        'type': 'telegram_rejected', 'status': status, 'recipient': recipient})
    r.raise_for_status()


async def event_outbox(webapp):
    queue = await asyncio.to_thread(Outbox, config.OUTBOX_PATH)
    webapp[OUTBOX_KEY] = queue
    worker = asyncio.create_task(queue.run(api.send, report_rejected))
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


def health(**state):
    """GET /health без поллинга: нет токена или Telegram его отверг. Процесс жив
    и ждёт токен из панели — для docker HEALTHCHECK это здоровье."""
    async def handle(_request: web.Request) -> web.Response:
        return web.json_response({'ok': True, **state, 'waiting_token': True, 'username': ''})
    return handle


async def handle_health(_request: web.Request) -> web.Response:
    """GET /health — жив ли поллер. Дёргает docker HEALTHCHECK, слушаем только
    на 127.0.0.1, поэтому без секрета. 503, если getUpdates давно не отвечал.
    username - имя из getMe, его показывает панель в «Настройках»."""
    idle = round(time.monotonic() - api.LAST_POLL_OK, 1)
    stalled = config.POLL_STALL_SEC > 0 and idle > config.POLL_STALL_SEC
    return web.json_response(
        {'ok': not stalled, 'poll_idle_sec': idle, 'poll_stall_sec': config.POLL_STALL_SEC,
         'username': config.BOT_USERNAME or ''},
        status=503 if stalled else 200)


def chat_ids(values) -> list[int]:
    """Номера чатов из события; не список, пусто или не номер чата — ValueError."""
    if not isinstance(values, list) or not values:
        raise ValueError('no recipients')
    for value in values:
        if isinstance(value, bool) or not re.fullmatch(r'-?[0-9]{1,16}', str(value)) or not int(value):
            raise ValueError('bad chat id')
    return list(dict.fromkeys(map(int, values)))


def admin_note(text: str) -> list[dict]:
    return [{'chat_id': config.CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}]


def system_alert(data: dict) -> list[dict]:
    """Тревога системы (упал бэкап или задание по расписанию) — в админский чат."""
    subject, detail = data.get('subject', 'system alert'), data.get('detail', '')
    if not isinstance(subject, str) or not isinstance(detail, str):
        raise ValueError('bad alert')
    return admin_note(f'⚠️ {md_bold(subject)}' + (f'\n\n{md(detail)}' if detail else ''))


# Уведомления: событие → сообщения в очередь. Текст клиенту готовит панель,
# бот только доставляет; очередь переживает рестарт и недоступность Telegram.
QUEUED = {
    'client_created': lambda d: admin_note(texts.NEW_CLIENT.format(md_bold(d.get('name', '?')))),
    'broadcast': lambda d: [{'chat_id': chat_id, 'text': d.get('text', '')}
                            for chat_id in chat_ids(d.get('chatIds'))],
    'client_notify': lambda d: [{'chat_id': chat_ids([d.get('chatId')])[0],
                                 'text': d.get('text', '')}],
    'system_alert': system_alert,
}


async def send_wireguard(data: dict) -> None:
    """Два сообщения: QR картинкой (сканируется прямо из чата), затем .conf файлом."""
    name, conf = data.get('name', '?'), data.get('conf', '')
    if not conf:
        return
    caption = f'{md_bold(name)} — WireGuard'
    # QR приходит от панели как PNG в base64; панель прошлой версии его не шлёт.
    qr = b''
    with suppress(ValueError, TypeError):
        qr = base64.b64decode(data.get('qr'), validate=True)
    if qr.startswith(b'\x89PNG\r\n\x1a\n'):
        await api.send_file(config.CHAT_ID, 'photo', 'qr.png', qr, caption)
    await api.send_file(config.CHAT_ID, 'document', f'{name}.conf', conf.encode('utf-8'), caption)


async def send_openvpn(data: dict) -> None:
    # В .ovpn встроены сертификаты — в QR не помещается, только файлом.
    name, conf = data.get('name', '?'), data.get('conf', '')
    if conf:
        await api.send_file(config.CHAT_ID, 'document', f'{name}.ovpn', conf.encode('utf-8'),
                            f'{md_bold(name)} — OpenVPN')


async def send_ikev2(data: dict) -> None:
    """Данные для входа текстом, затем CA-сертификат сервера файлом."""
    username = data.get('username', '?')
    await api.send(config.CHAT_ID, texts.IKEV2.format(
        data.get('server', '?'), username, data.get('password', '?'), md_bold(username)), 'Markdown')
    if data.get('caCertPem'):
        await api.send_file(config.CHAT_ID, 'document', f'{username}-ca.crt',
                            data['caCertPem'].encode('utf-8'),
                            f'{md_bold(username)} — CA-сертификат IKEv2')


async def send_invite(data: dict) -> None:
    """Пояснение админу и готовое приглашение, которое он пересылает клиенту."""
    if data.get('password', ''):
        await api.send(config.CHAT_ID, texts.INVITE_NOTE.format(data.get('name', '?')))
        await api.send(config.CHAT_ID, texts.invite(data['password']))


async def announce_deploy(data: dict) -> None:
    unmeasured = data.get('verify_unmeasured') is True
    text = title = texts.DEPLOY_WARNED if unmeasured else texts.DEPLOY_DONE
    # Панель сразу после выкатки может ещё прогреваться — несколько попыток.
    for attempt in range(5):
        try:
            text = texts.status(await api.status(), title)
            break
        except Exception as e:
            log.warning('announce_deploy: status failed (try %d): %s', attempt + 1, e)
            await asyncio.sleep(3)
    await api.send(config.CHAT_ID, text + (texts.DEPLOY_UNMEASURED if unmeasured else ''), 'Markdown')


# Выдача по кнопке панели уходит в админский чат сразу, мимо очереди:
# админ пересылает её клиенту сам.
SENT = {
    'client_send_wireguard': send_wireguard,
    'client_send_openvpn': send_openvpn,
    'client_send_ikev2': send_ikev2,
    'client_send_password': send_invite,
    'deploy_done': announce_deploy,
}


async def handle_event(request: web.Request) -> web.Response:
    # Сравнение за постоянное время: по длительности ответа секрет не подобрать.
    provided = request.headers.get('X-Tgbot-Secret', '')
    if not hmac.compare_digest(provided.encode(), config.SECRET.encode()):
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
        event_id = await asyncio.to_thread(
            request.app[OUTBOX_KEY].enqueue, payloads, delivery_id)
    except (ValueError, UnicodeError):
        return web.Response(status=400, text='invalid notification')
    except (QueueFull, sqlite3.Error, OSError):
        log.error('outbox cannot accept notification')
        return web.Response(status=503, text='notification queue unavailable')
    return web.json_response({'ok': True, 'event_id': event_id, 'delivery_id': delivery_id})
