import asyncio
import hmac
import os
import re
import sqlite3
import time
from contextlib import suppress

from aiohttp import web

from . import config, monitor, panel, transport
from .config import log
from .outbox import Outbox, QueueFull
from .texts import md, md_bold


OUTBOX_KEY = web.AppKey('outbox', Outbox)


async def report_rejected(payload: dict, status: int) -> None:
    """Отказ Telegram — тревога в журнал панели. В Telegram бот об этом не пишет."""
    recipient = 'admin' if payload.get('chat_id') == config.CHAT_ID else 'client'
    r = await transport._local.post('/api/bot/events', headers=panel._bot_headers(),
                          json={'type': 'telegram_rejected', 'status': status, 'recipient': recipient})
    r.raise_for_status()


async def event_outbox(webapp):
    queue = await asyncio.to_thread(
        Outbox, os.environ.get('TGBOT_OUTBOX_PATH', '/var/lib/anysda-tgbot/outbox.sqlite'))
    webapp[OUTBOX_KEY] = queue
    worker = asyncio.create_task(queue.run(transport.tg_send, report_rejected))
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


async def enqueue_notification(request, payload, delivery_id=None):
    try:
        queue = request.app[OUTBOX_KEY]
        enqueue = queue.enqueue_batch if isinstance(payload, list) else queue.enqueue
        event_id = await asyncio.to_thread(enqueue, payload, delivery_id)
    except (ValueError, UnicodeError):
        return web.Response(status=400, text='invalid notification')
    except (QueueFull, sqlite3.Error, OSError):
        log.error('outbox cannot accept notification')
        return web.Response(status=503, text='notification queue unavailable')
    return web.json_response({'ok': True, 'event_id': event_id, 'delivery_id': delivery_id})


async def handle_health(_request: web.Request) -> web.Response:
    """GET /health — жив ли поллер. Дёргает docker HEALTHCHECK, слушаем только
    на 127.0.0.1, поэтому без секрета. 503, если getUpdates давно не отвечал.
    username - имя из getMe, его показывает панель в «Настройках»."""
    idle = round(time.monotonic() - transport.LAST_POLL_OK, 1)
    stalled = config.POLL_STALL_SEC > 0 and idle > config.POLL_STALL_SEC
    return web.json_response(
        {'ok': not stalled, 'poll_idle_sec': idle, 'poll_stall_sec': config.POLL_STALL_SEC,
         'username': config.BOT_USERNAME or ''},
        status=503 if stalled else 200,
    )


async def handle_health_waiting(_request: web.Request) -> web.Response:
    """GET /health, пока нет токена или Chat ID: процесс жив и ждёт панель, это
    здоровье для docker HEALTHCHECK. waiting_token отличает его от рабочего бота."""
    return web.json_response({'ok': True, 'waiting_token': True, 'username': ''})


async def handle_health_invalid_token(_request: web.Request) -> web.Response:
    # Пауза после отвергнутого токена: процесс жив и ждёт новый токен из панели.
    return web.json_response({'ok': True, 'invalid_token': True, 'waiting_token': True, 'username': ''})


async def handle_event(request: web.Request) -> web.Response:
    # SECRET гарантированно непустой (startup-check выше). Сравниваем константно
    # по времени, чтобы убрать timing-leak; encode в bytes — compare_digest требует
    # одинаковый тип на обоих аргументах.
    provided = request.headers.get('X-Tgbot-Secret', '')
    if not hmac.compare_digest(provided.encode(), config.SECRET.encode()):
        return web.Response(status=403)
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)
    if not isinstance(data, dict):
        return web.Response(status=400)
    evt = data.get('type')
    delivery_id = data.get('deliveryId')
    if evt == 'client_created':
        name = data.get('name', '?')
        return await enqueue_notification(request, {
            'chat_id': config.CHAT_ID, 'text': f'👤 Новый клиент: {md_bold(name)}', 'parse_mode': 'Markdown',
        }, delivery_id)
    elif evt == 'broadcast':
        chat_ids = data.get('chatIds')
        if (not isinstance(chat_ids, list) or not chat_ids
                or any(isinstance(chat_id, bool) or not re.fullmatch(r'-?[0-9]{1,16}', str(chat_id))
                       or not int(chat_id) for chat_id in chat_ids)):
            return web.Response(status=400)
        return await enqueue_notification(request, [
            {'chat_id': chat_id, 'text': data.get('text', '')}
            for chat_id in dict.fromkeys(map(int, chat_ids))
        ], delivery_id)
    elif evt == 'client_send_wireguard':
        name = data.get('name', '?')
        conf = data.get('conf', '')
        if conf:
            # WireGuard: два сообщения — сначала QR как фото, затем .conf файлом.
            async def _send_wg():
                await transport.tg_send_qr(config.CHAT_ID, conf, f'{md_bold(name)} — WireGuard')
                await transport.tg_send_document(
                    config.CHAT_ID, f'{name}.conf', conf.encode('utf-8'),
                    caption=f'{md_bold(name)} — WireGuard',
                )
            asyncio.create_task(_send_wg())
    elif evt == 'client_send_openvpn':
        name = data.get('name', '?')
        conf = data.get('conf', '')
        if conf:
            # .ovpn carries inline certs — too large for a QR, send as a file.
            asyncio.create_task(transport.tg_send_document(
                config.CHAT_ID, f'{name}.ovpn', conf.encode('utf-8'),
                caption=f'{md_bold(name)} — OpenVPN',
            ))
    elif evt == 'client_send_ikev2':
        # IKEv2 (EAP-MSCHAPv2 + серверный CA). Шлём в АДМИНСКИЙ чат —
        # админ пересылает клиенту. Поведение зависит от платформы:
        #   ios     → 1 сообщение: .mobileconfig файлом (всё внутри)
        #   android → 4 сообщения: server / login / password / ca.crt
        #   windows → 4 сообщения: server / login / password / ca.crt
        asyncio.create_task(_send_ikev2(data))
    elif evt == 'client_send_password':
        # Приглашение клиенту: бот шлёт в АДМИНСКИЙ чат два сообщения —
        # (1) пояснение админу, (2) готовое приветствие с диплинком, которое
        # админ пересылает клиенту.
        name = data.get('name', '?')
        password = data.get('password', '')
        if password:
            asyncio.create_task(_send_invite(name, password))
    elif evt == 'client_notify':
        # Уведомление привязанному клиенту об изменении его аккаунта
        # (квота, заморозка, девайсы, срок, перевыпуск ключей). Текст
        # готовит панель — бот просто доставляет в клиентский чат.
        chat_id = data.get('chatId')
        text = data.get('text', '')
        if isinstance(chat_id, bool) or not re.fullmatch(r'-?[0-9]{1,16}', str(chat_id)):
            return web.Response(status=400)
        if not int(chat_id):
            return web.Response(status=400)
        return await enqueue_notification(request, {'chat_id': int(chat_id), 'text': text}, delivery_id)
    elif evt == 'deploy_done':
        asyncio.create_task(_announce_deploy(data.get('verify_unmeasured') is True))
    elif evt == 'system_alert':
        # Алерт от системы (backup-фейл, scheduled job упал и т.п.) →
        # в админский чат. subject/detail — текстовые поля, без markup.
        subject = data.get('subject', 'system alert')
        detail = data.get('detail', '')
        if not isinstance(subject, str) or not isinstance(detail, str):
            return web.Response(status=400)
        text = f'⚠️ {md_bold(subject)}' + (f'\n\n{md(detail)}' if detail else '')
        return await enqueue_notification(request, {
            'chat_id': config.CHAT_ID, 'text': text, 'parse_mode': 'Markdown',
        }, delivery_id)
    return web.Response(text='ok')


async def _send_invite(name: str, password: str) -> None:
    """Два сообщения в админский чат: пояснение + готовое приглашение клиенту."""
    await transport.tg_send(
        config.CHAT_ID,
        f'📨 Приглашение для «{name}». Перешлите клиенту сообщение ниже ↓',
    )
    if config.BOT_USERNAME:
        deeplink = f'https://t.me/{config.BOT_USERNAME}?start={password}'
        invite = (
            '🔐 Вам предоставлен доступ к VPN. '
            'Откройте ссылку и нажмите «Запустить»:\n'
            f'{deeplink}'
        )
    else:
        # Фолбэк: username бота неизвестен — даём текстовую инструкцию.
        invite = (
            '🔐 Вам предоставлен доступ к VPN.\n'
            'Найдите нашего бота в Telegram и отправьте ему сообщение:\n'
            f'/start {password}'
        )
    await transport.tg_send(config.CHAT_ID, invite)


async def _send_ikev2(data: dict) -> None:
    """IKEv2 → админский чат. Два сообщения (как WG: QR + .conf):
       1) текст с server/login/password + caption '<username> — IKEv2'
       2) ca.crt файлом + caption '<username> — CA-сертификат IKEv2'
    """
    username = data.get('username', '?')
    server = data.get('server', '?')
    password = data.get('password', '?')
    ca_pem = data.get('caCertPem') or ''

    creds = (
        f'Сервер: `{server}`\n'
        f'Логин: `{username}`\n'
        f'Пароль: `{password}`\n\n'
        f'{md_bold(username)} — IKEv2'
    )
    await transport.tg_send(config.CHAT_ID, creds, 'Markdown')
    if ca_pem:
        await transport.tg_send_document(
            config.CHAT_ID, f'{username}-ca.crt', ca_pem.encode('utf-8'),
            caption=f'{md_bold(username)} — CA-сертификат IKEv2',
        )


async def _announce_deploy(verify_unmeasured: bool = False) -> None:
    # Nuxt API может ещё прогреваться сразу после 30-frontend — ретраим пару раз.
    title = ('⚠️ *Установка завершена с предупреждениями*' if verify_unmeasured
             else '✅ *Установка завершена*')
    text = title
    for attempt in range(5):
        try:
            text = await monitor.build_status_text(title)
            break
        except Exception as e:
            log.warning('announce_deploy: api_status failed (try %d): %s', attempt + 1, e)
            await asyncio.sleep(3)
    if verify_unmeasured:
        text += '\n\n? Не проверено: часть проверок не измерена. Причины — в сводке деплоя 99-verify.'
    await transport.tg_send(config.CHAT_ID, text, 'Markdown')
