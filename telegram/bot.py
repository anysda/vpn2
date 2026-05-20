#!/usr/bin/env python3
"""
anysda-vpn Telegram bot.

Commands:
  /status   — system overview (servers + outbounds RTT)
  /nodes    — per-exit-node RTT
  /clients  — WG client list

Background: monitors outbound health every 60 s.
  Sends alert if RTT=0 for ≥3 consecutive checks (~3 min).
  Clears alert when node recovers.

Listens on TGBOT_EVENT_PORT for events from Nuxt:
  POST /event  {"type": "client_created", "name": "..."}

All Telegram traffic goes through SOCKS5_PROXY
(sing-box local SOCKS5 on 127.0.0.1:7897 → foreign-best exit).

Env vars:
  TELEGRAM_BOT_TOKEN  — required
  TELEGRAM_CHAT_ID    — required (integer)
  TGBOT_SECRET        — shared secret with Nuxt (Bearer token)
  TGBOT_EVENT_PORT    — HTTP port for Nuxt→bot events (default 8877)
  ANYSDA_URL          — Nuxt API base (default http://127.0.0.1:51821)
  SOCKS5_PROXY        — socks5://127.0.0.1:7897
"""

import asyncio
import io
import logging
import os

import httpx
import qrcode
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s — %(message)s',
)
log = logging.getLogger('tgbot')

# Runtime config поверх env-vars: если файл существует — он перебивает токен/чат_id.
# Используется панелью (Боты): при сохранении в UI Nuxt пишет сюда, бот сам
# делает sys.exit() при изменении mtime, docker --restart unless-stopped
# перезапускает контейнер с новыми значениями.
import json
import pathlib
import sys
RUNTIME_PATH = pathlib.Path('/etc/anysda/telegram-runtime.json')


def _load_token_chat() -> tuple[str, int]:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat = os.environ.get('TELEGRAM_CHAT_ID', '0')
    if RUNTIME_PATH.exists():
        try:
            data = json.loads(RUNTIME_PATH.read_text())
            token = (data.get('bot_token') or token).strip()
            chat = str(data.get('chat_id') or chat).strip()
        except Exception:
            pass
    if not token:
        raise RuntimeError('TELEGRAM_BOT_TOKEN не задан (ни env, ни runtime.json)')
    return token, int(chat)


TOKEN, CHAT_ID = _load_token_chat()
SECRET     = os.environ.get('TGBOT_SECRET', '')
EVENT_PORT = int(os.environ.get('TGBOT_EVENT_PORT', '8877'))
ANYSDA_URL = os.environ.get('ANYSDA_URL', 'http://127.0.0.1:51821')
SOCKS5     = os.environ.get('SOCKS5_PROXY', 'socks5://127.0.0.1:7897')

# Local client — for Nuxt API on 127.0.0.1
_local = httpx.AsyncClient(base_url=ANYSDA_URL, timeout=5.0)

# Telegram API напрямую — обходит python-telegram-bot pool (получали PoolTimeout
# даже с pool_size=50). Этот клиент используется ТОЛЬКО для send_message,
# polling getUpdates делает Application через свой HTTPXRequest.
_tg = httpx.AsyncClient(
    base_url=f'https://api.telegram.org/bot{TOKEN}',
    timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)


async def tg_send(chat_id: int, text: str, parse_mode: str | None = None) -> dict:
    payload: dict = {'chat_id': chat_id, 'text': text}
    if parse_mode:
        payload['parse_mode'] = parse_mode
    r = await _tg.post('/sendMessage', json=payload, timeout=20)
    return r.json()


def _make_qr_png(data: str) -> bytes:
    """Render an ss:// URL into a PNG QR — black on white, big margin so phones lock onto it."""
    img = qrcode.make(data, box_size=10, border=4)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


async def tg_send_qr(chat_id: int, ss_url: str, caption: str) -> dict:
    """Send a QR PNG with caption as photo — phones can scan straight from chat."""
    png = _make_qr_png(ss_url)
    files = {'photo': ('qr.png', png, 'image/png')}
    data = {'chat_id': str(chat_id), 'caption': caption, 'parse_mode': 'Markdown'}
    r = await _tg.post('/sendPhoto', data=data, files=files, timeout=30)
    return r.json()


async def tg_send_document(chat_id: int, filename: str, content: bytes, caption: str = '') -> dict:
    """Send raw bytes as a Telegram document (used for .conf delivery)."""
    files = {'document': (filename, content, 'application/octet-stream')}
    data: dict = {'chat_id': str(chat_id)}
    if caption:
        data['caption'] = caption
        data['parse_mode'] = 'Markdown'
    r = await _tg.post('/sendDocument', data=data, files=files, timeout=30)
    return r.json()


async def tg_send_media_group(chat_id: int, items: list[tuple[str, bytes, str]],
                              caption: str = '') -> dict:
    """Send several files as ONE grouped message (media group of documents).

    Telegram не позволяет смешивать photo и document в одной media group, поэтому
    QR уходит тоже как document — так WireGuard-конфиг и QR приходят одним
    сообщением. items: список (filename, content, mime). caption — на первом файле.
    """
    media: list[dict] = []
    files: dict = {}
    for i, (filename, content, mime) in enumerate(items):
        key = f'file{i}'
        files[key] = (filename, content, mime)
        m: dict = {'type': 'document', 'media': f'attach://{key}'}
        if i == 0 and caption:
            m['caption'] = caption
            m['parse_mode'] = 'Markdown'
        media.append(m)
    data = {'chat_id': str(chat_id), 'media': json.dumps(media)}
    r = await _tg.post('/sendMediaGroup', data=data, files=files, timeout=40)
    return r.json()


async def tg_reply(update: Update, text: str, parse_mode: str | None = None) -> dict:
    return await tg_send(update.effective_chat.id, text, parse_mode)


async def tg_edit(chat_id: int, message_id: int, text: str, parse_mode: str | None = None) -> dict:
    payload: dict = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
    if parse_mode:
        payload['parse_mode'] = parse_mode
    r = await _tg.post('/editMessageText', json=payload, timeout=20)
    return r.json()

# Down-count tracking per outbound tag
_down: dict[str, int] = {}
_alerted: set[str] = set()


async def api_status() -> dict:
    headers = {'Authorization': f'Bearer {SECRET}'} if SECRET else {}
    r = await _local.get('/api/ops/bot', headers=headers)
    r.raise_for_status()
    return r.json()


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await tg_reply(
        update,
        '*anysda-vpn*\n\n'
        '/status — состояние системы\n'
        '/nodes  — RTT по exit-нодам\n'
        '/clients — WG клиенты',
        'Markdown',
    )


async def build_status_text(header: str = '*Состояние системы*') -> str:
    data = await api_status()
    lines = [header + '\n']
    for s in data.get('servers', []):
        net = s.get('rxMbps', 0) + s.get('txMbps', 0)
        lines.append(
            f"🖥 *{s['tag'].upper()}*: CPU {s['cpuPct']:.0f}%"
            f" | RAM {s['ramPct']:.0f}%"
            f" | ↕ {net:.1f} Mbps"
        )
    lines.append('')
    for o in data.get('outbounds', []):
        icon = '🟢' if o['status'] == 'up' else '🔴'
        kbps = o.get('throughputKbps', 0)
        lines.append(f"{icon} `{o['label']}`: {o['rttMs']} ms | {kbps} Kbps")
    return '\n'.join(lines)


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await tg_reply(update, await build_status_text(), 'Markdown')
    except Exception as e:
        log.exception('cmd_status failed')
        await tg_reply(update, f'❌ Ошибка: {e}')


async def cmd_nodes(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = await api_status()
        lines = ['*Exit-ноды*\n']
        for o in data.get('outbounds', []):
            if o['tag'] == 'direct-ru':
                continue
            icon = '🟢' if o['status'] == 'up' else '🔴'
            lines.append(f"{icon} `{o['label']}`: {o['rttMs']} ms")
        await tg_reply(
            update,
            '\n'.join(lines) if len(lines) > 1 else 'Нет нод',
            'Markdown',
        )
    except Exception as e:
        log.exception('cmd_nodes failed')
        await tg_reply(update, f'❌ Ошибка: {e}')


async def cmd_clients(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = await api_status()
        clients = data.get('clients', [])
        lines = [f'*WG клиенты* ({len(clients)} шт.)\n']
        for c in clients:
            icon = '✅' if c.get('enabled') else '❌'
            lines.append(f"{icon} `{c.get('name', '?')}`")
        await tg_reply(
            update,
            '\n'.join(lines) if clients else 'Нет клиентов',
            'Markdown',
        )
    except Exception as e:
        log.exception('cmd_clients failed')
        await tg_reply(update, f'❌ Ошибка: {e}')


# ── Background monitor ────────────────────────────────────────────────────────

# Алерты: проверки каждые MONITOR_INTERVAL сек, ALERT_HITS подряд = триггер.
# Снятие — когда метрика опускается ниже (порог - HYSTERESIS).
# По умолчанию: 30с * 2 = ~60с до алерта.
MONITOR_INTERVAL = int(os.environ.get('ALERT_INTERVAL_SEC', '30'))
ALERT_HITS       = int(os.environ.get('ALERT_HITS',         '2'))
CPU_HIGH = float(os.environ.get('ALERT_CPU_PCT', '80'))
RAM_HIGH = float(os.environ.get('ALERT_RAM_PCT', '80'))
HYSTERESIS = 10.0

_load_hits: dict[str, int] = {}     # key = f'{tag}:cpu' / f'{tag}:ram'
_load_alerted: set[str] = set()


async def _check_load(tag: str, metric: str, value: float, threshold: float,
                       label: str, unit: str = '%') -> None:
    key = f'{tag}:{metric}'
    if value >= threshold:
        _load_hits[key] = _load_hits.get(key, 0) + 1
        if _load_hits[key] >= ALERT_HITS and key not in _load_alerted:
            _load_alerted.add(key)
            await tg_send(
                CHAT_ID,
                f'⚠️ *{tag.upper()}*: {label} {value:.0f}{unit} ≥ {threshold:.0f}{unit} '
                f'(порог {ALERT_HITS}× по {MONITOR_INTERVAL}с)',
                'Markdown',
            )
    elif value < threshold - HYSTERESIS:
        if key in _load_alerted:
            _load_alerted.discard(key)
            await tg_send(
                CHAT_ID,
                f'✅ *{tag.upper()}*: {label} вернулся в норму ({value:.0f}{unit})',
                'Markdown',
            )
        _load_hits[key] = 0


async def monitor_loop(app: Application) -> None:
    log.info('monitor_loop started: interval=%ds, hits=%d, cpu_high=%.0f, ram_high=%.0f',
             MONITOR_INTERVAL, ALERT_HITS, CPU_HIGH, RAM_HIGH)
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        try:
            data = await api_status()

            # Outbound up/down
            for o in data.get('outbounds', []):
                tag = o['tag']
                if tag == 'direct-ru':
                    continue
                label = o.get('label', tag)
                if o['status'] == 'down':
                    _down[tag] = _down.get(tag, 0) + 1
                    if _down[tag] >= ALERT_HITS and tag not in _alerted:
                        _alerted.add(tag)
                        await tg_send(
                            CHAT_ID,
                            f'🔴 *Нода {label} недоступна* '
                            f'(порог {ALERT_HITS}× по {MONITOR_INTERVAL}с)',
                            'Markdown',
                        )
                else:
                    if tag in _alerted:
                        _alerted.discard(tag)
                        await tg_send(
                            CHAT_ID,
                            f'🟢 *Нода {label} восстановлена*',
                            'Markdown',
                        )
                    _down[tag] = 0

            # CPU / RAM на каждой ноде
            for s in data.get('servers', []):
                tag = s.get('tag', '?')
                cpu = float(s.get('cpuPct', 0))
                ram = float(s.get('ramPct', 0))
                await _check_load(tag, 'cpu', cpu, CPU_HIGH, 'CPU')
                await _check_load(tag, 'ram', ram, RAM_HIGH, 'RAM')
        except Exception as e:
            log.warning('monitor error: %s', e)


# ── HTTP server for Nuxt events ───────────────────────────────────────────────

async def handle_event(request: web.Request) -> web.Response:
    if SECRET and request.headers.get('X-Tgbot-Secret') != SECRET:
        return web.Response(status=403)
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)
    evt = data.get('type')
    if evt == 'client_created':
        name = data.get('name', '?')
        asyncio.create_task(
            tg_send(
                CHAT_ID,
                f'👤 Новый клиент: *{name}*',
                'Markdown',
            )
        )
    elif evt == 'client_send_config':
        name = data.get('name', '?')
        ss_url = data.get('ssUrl', '')
        if ss_url:
            # Outline: только ключ текстом, без QR.
            asyncio.create_task(tg_send(
                CHAT_ID, f'*{name}* — Outline\n\n`{ss_url}`', 'Markdown',
            ))
    elif evt == 'client_send_wireguard':
        name = data.get('name', '?')
        conf = data.get('conf', '')
        if conf:
            # WireGuard: QR + .conf одним сообщением (media group из двух документов).
            qr_png = _make_qr_png(conf)
            asyncio.create_task(tg_send_media_group(
                CHAT_ID,
                [
                    (f'{name}.conf', conf.encode('utf-8'), 'application/octet-stream'),
                    (f'{name}-qr.png', qr_png, 'image/png'),
                ],
                caption=f'*{name}* — WireGuard',
            ))
    elif evt == 'client_send_openvpn':
        name = data.get('name', '?')
        conf = data.get('conf', '')
        if conf:
            # .ovpn carries inline certs — too large for a QR, send as a file.
            asyncio.create_task(tg_send_document(
                CHAT_ID, f'{name}.ovpn', conf.encode('utf-8'),
                caption=f'*{name}* — OpenVPN',
            ))
    elif evt == 'deploy_done':
        asyncio.create_task(_announce_deploy())
    return web.Response(text='ok')


async def _announce_deploy() -> None:
    # Nuxt API может ещё прогреваться сразу после 30-frontend — ретраим пару раз.
    text = '✅ *Установка завершена*'
    for attempt in range(5):
        try:
            text = await build_status_text('✅ *Установка завершена*')
            break
        except Exception as e:
            log.warning('announce_deploy: api_status failed (try %d): %s', attempt + 1, e)
            await asyncio.sleep(3)
    await tg_send(CHAT_ID, text, 'Markdown')


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    # Telegram API доступен с RU напрямую (на этом хостинге не блокируется)
    # — ходим без SOCKS5. httpx-socks через sing-box создавал ложные
    # PoolTimeout даже при connection_pool_size=8: похоже SOCKS5-туннель
    # сериализует requests через single CONNECT, и keep-alive ломается.
    # Если в дальнейшем нужно будет скрыть RU IP — добавим обратно прокси
    # и разберёмся с httpx-socks отдельно.
    # connection_pool_size + pool_timeout default'ы в HTTPXRequest очень
    # маленькие (1 и 1.0с) — при любой задержке handler-команд получаем
    # PoolTimeout. Поднимаем с запасом, чтобы команды + monitor_loop +
    # event_server жили вместе.
    poll_req = HTTPXRequest(connection_pool_size=4, read_timeout=40)
    send_req = HTTPXRequest(
        connection_pool_size=50,
        pool_timeout=20.0,
        connect_timeout=10.0,
        read_timeout=20.0,
    )
    tgapp = (
        Application.builder()
        .token(TOKEN)
        .request(send_req)
        .get_updates_request(poll_req)
        .build()
    )
    tgapp.add_handler(CommandHandler('start',   cmd_start))
    tgapp.add_handler(CommandHandler('help',    cmd_start))
    tgapp.add_handler(CommandHandler('status',  cmd_status))
    tgapp.add_handler(CommandHandler('nodes',   cmd_nodes))
    tgapp.add_handler(CommandHandler('clients', cmd_clients))

    # aiohttp event server for Nuxt callbacks
    webapp = web.Application()
    webapp['tgapp'] = tgapp
    webapp.router.add_post('/event', handle_event)
    runner = web.AppRunner(webapp)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', EVENT_PORT).start()
    log.info('Event server on 127.0.0.1:%d', EVENT_PORT)

    async def _supervised_monitor():
        try:
            await monitor_loop(tgapp)
        except Exception:
            log.exception('monitor_loop crashed')

    # Если UI редактирует /etc/anysda/telegram-runtime.json — exit, docker
    # рестартит контейнер с новыми token/chat_id.
    async def _config_watcher():
        initial_mtime = RUNTIME_PATH.stat().st_mtime if RUNTIME_PATH.exists() else None
        while True:
            await asyncio.sleep(5)
            mtime = RUNTIME_PATH.stat().st_mtime if RUNTIME_PATH.exists() else None
            if mtime != initial_mtime:
                log.info('telegram-runtime.json изменён — выхожу для рестарта')
                os._exit(0)

    async with tgapp:
        await tgapp.start()
        asyncio.create_task(_supervised_monitor())
        asyncio.create_task(_config_watcher())
        await tgapp.updater.start_polling(drop_pending_updates=True)
        log.info('Bot started, polling Telegram via %s', SOCKS5)
        await asyncio.Event().wait()  # run forever
        await tgapp.updater.stop()
        await tgapp.stop()

    await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
