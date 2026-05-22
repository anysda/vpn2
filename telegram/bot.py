#!/usr/bin/env python3
"""
anysda-vpn Telegram bot.

Мультипользовательский: обслуживает админа (чат TELEGRAM_CHAT_ID) и
конечных клиентов VPN (любой другой чат — самообслуживание).

Админ-команды (только из админского чата):
  /status   — system overview (nodes CPU/RAM + outbounds RTT)
  /nodes    — per-exit-node RTT
  /clients  — client list

Клиент (любой другой чат):
  /start <пароль>  — привязать чат к клиенту по диплинку-приглашению
  /start           — открыть меню (если чат уже привязан)
  инлайн-меню — устройства, выдача WG/OVPN конфигов, перевыпуск, удаление

Background monitor (every ALERT_INTERVAL_SEC, default 30 s) — только админу:
  - alerts when a node's metrics go stale for > ALERT_NODE_DOWN_SEC
    (default 5 min) — i.e. the node/VM is down;
  - alerts on high CPU / RAM.
  Each alert is cleared when the node recovers.

Listens on TGBOT_EVENT_PORT for events from Nuxt:
  POST /event  {"type": "client_created", "name": "..."}

Telegram API traffic egresses through an exit node: the RU entry node
in Moscow can't reach api.telegram.org directly, so the bot's
Telegram-facing httpx clients route through the local HTTP proxy that
sing-box exposes (→ foreign-best → exit). Only calls to the Nuxt API on
127.0.0.1 stay direct.

Env vars:
  TELEGRAM_BOT_TOKEN  — required
  TELEGRAM_CHAT_ID    — required (integer)
  TGBOT_SECRET        — shared secret with Nuxt (Bearer token)
  TGBOT_EVENT_PORT    — HTTP port for Nuxt→bot events (default 8877)
  ANYSDA_URL          — Nuxt API base (default http://127.0.0.1:51821)
  TG_PROXY            — HTTP proxy for Telegram API (default http://127.0.0.1:7897)
"""

import asyncio
import io
import json
import logging
import os
import pathlib

import httpx
import qrcode
from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s — %(message)s',
)
log = logging.getLogger('tgbot')

# Runtime config поверх env-vars: если файл существует — он перебивает токен/чат_id.
# Используется панелью (Боты): при сохранении в UI Nuxt пишет сюда, бот сам
# делает os._exit(0) при изменении mtime, docker --restart unless-stopped
# перезапускает контейнер с новыми значениями.
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
# Telegram API egress: RU-нода в Москве api.telegram.org напрямую не достаёт,
# поэтому Telegram-клиенты бота ходят через этот локальный HTTP-прокси
# (sing-box mixed-инбаунд → foreign-best → экзит). Обращения к Nuxt API на
# 127.0.0.1 проксировать НЕ нужно — _local идёт напрямую.
TG_PROXY   = os.environ.get('TG_PROXY', 'http://127.0.0.1:7897')

# username бота — заполняется в main() через getMe; нужен для диплинков-приглашений.
BOT_USERNAME: str | None = None

# Local client — for Nuxt API on 127.0.0.1 (direct, no proxy)
_local = httpx.AsyncClient(base_url=ANYSDA_URL, timeout=10.0)

# Отдельный httpx-клиент для send-методов Telegram — в обход пула
# python-telegram-bot (с ним ловили PoolTimeout). polling getUpdates идёт
# через свой HTTPXRequest в Application. Оба ходят через TG_PROXY → экзит.
_tg = httpx.AsyncClient(
    base_url=f'https://api.telegram.org/bot{TOKEN}',
    proxy=TG_PROXY,
    timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)


# ── Telegram API helpers (raw httpx через TG_PROXY) ────────────────────────────

async def tg_send(chat_id: int, text: str, parse_mode: str | None = None,
                   reply_markup: dict | None = None) -> dict:
    payload: dict = {'chat_id': chat_id, 'text': text}
    if parse_mode:
        payload['parse_mode'] = parse_mode
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    r = await _tg.post('/sendMessage', json=payload, timeout=20)
    return r.json()


async def tg_edit(chat_id: int, message_id: int, text: str,
                  parse_mode: str | None = None,
                  reply_markup: dict | None = None) -> dict:
    """editMessageText — для перерисовки инлайн-меню без новых сообщений."""
    payload: dict = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
    if parse_mode:
        payload['parse_mode'] = parse_mode
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    r = await _tg.post('/editMessageText', json=payload, timeout=20)
    return r.json()


async def tg_answer_callback(callback_id: str, text: str = '',
                             show_alert: bool = False) -> dict:
    """answerCallbackQuery — гасит «часики» на нажатой инлайн-кнопке."""
    payload: dict = {'callback_query_id': callback_id}
    if text:
        payload['text'] = text
    if show_alert:
        payload['show_alert'] = True
    r = await _tg.post('/answerCallbackQuery', json=payload, timeout=20)
    return r.json()


def _make_qr_png(data: str) -> bytes:
    """Render a config string into a PNG QR — black on white, big margin so phones lock onto it."""
    img = qrcode.make(data, box_size=10, border=4)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


async def tg_send_qr(chat_id: int, payload: str, caption: str) -> dict:
    """Send a QR PNG with caption as photo — phones can scan straight from chat."""
    png = _make_qr_png(payload)
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


async def tg_reply(update: Update, text: str, parse_mode: str | None = None,
                   reply_markup: dict | None = None) -> dict:
    return await tg_send(update.effective_chat.id, text, parse_mode, reply_markup)


# Счётчики «нода лежит» — по tag ноды: сколько проверок подряд она down
# и для каких уже отправлен алерт (чтобы не спамить).
_node_down: dict[str, int] = {}
_node_alerted: set[str] = set()


async def api_status() -> dict:
    headers = {'Authorization': f'Bearer {SECRET}'} if SECRET else {}
    r = await _local.get('/api/ops/bot-snapshot', headers=headers)
    r.raise_for_status()
    return r.json()


def _ob_label(name: str) -> str:
    """hy2-us-direct → US, hy2-de-warp → DE WARP."""
    s = name[4:] if name.startswith('hy2-') else name
    if s.endswith('-warp'):
        return s[:-5].upper() + ' WARP'
    if s.endswith('-direct'):
        return s[:-7].upper()
    return s.upper()


def _pct(v) -> str:
    return f'{v:.0f}%' if isinstance(v, (int, float)) else '—'


# ── Client API (Nuxt /api/bot/*) ───────────────────────────────────────────────

class BotApiError(Exception):
    """Ошибка вызова /api/bot/* — message человекочитаемый, для показа клиенту."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _bot_headers() -> dict:
    return {'Authorization': f'Bearer {SECRET}'} if SECRET else {}


def _extract_error(r: httpx.Response) -> BotApiError:
    """Из 4xx-ответа Nuxt вытащить человекочитаемый текст."""
    msg = ''
    try:
        body = r.json()
        if isinstance(body, dict):
            msg = body.get('message') or body.get('statusMessage') or body.get('error') or ''
    except Exception:
        msg = (r.text or '').strip()
    if not msg:
        msg = f'Ошибка сервера ({r.status_code})'
    return BotApiError(r.status_code, msg)


async def _bot_request(method: str, path: str, *, json_body: dict | None = None) -> httpx.Response:
    r = await _local.request(method, path, headers=_bot_headers(), json=json_body)
    if r.status_code >= 400:
        raise _extract_error(r)
    return r


async def api_link(chat_id: int, password: str, username: str | None) -> dict:
    body: dict = {'password': password, 'chatId': chat_id}
    if username:
        body['username'] = username
    r = await _bot_request('POST', '/api/bot/link', json_body=body)
    return r.json()


async def api_client(chat_id: int) -> dict:
    r = await _bot_request('GET', f'/api/bot/client?chatId={chat_id}')
    return r.json()


async def api_add_device(chat_id: int, name: str) -> dict:
    r = await _bot_request('POST', '/api/bot/devices',
                           json_body={'chatId': chat_id, 'name': name})
    return r.json()


async def api_delete_device(chat_id: int, device_id: str) -> dict:
    r = await _bot_request('DELETE', f'/api/bot/devices/{device_id}?chatId={chat_id}')
    return r.json()


async def api_reissue_device(chat_id: int, device_id: str) -> dict:
    r = await _bot_request('POST', f'/api/bot/devices/{device_id}/reissue',
                           json_body={'chatId': chat_id})
    return r.json()


async def api_wg_config(chat_id: int, device_id: str) -> str:
    r = await _bot_request('GET', f'/api/bot/devices/{device_id}/wg-config?chatId={chat_id}')
    return r.text


async def api_ovpn_config(chat_id: int, device_id: str) -> str:
    r = await _bot_request('GET', f'/api/bot/devices/{device_id}/ovpn-config?chatId={chat_id}')
    return r.text


# ── Client menu rendering ──────────────────────────────────────────────────────

# Чаты, ожидающие ввода названия устройства (после нажатия «Добавить устройство»).
# Простое in-memory состояние — переживать рестарт не нужно.
_awaiting_device_name: set[int] = set()


def _limit_label(device_limit) -> str:
    return '∞' if device_limit is None else str(device_limit)


def _menu_markup() -> dict:
    return {'inline_keyboard': [[
        {'text': '📱 Мои устройства', 'callback_data': 'devs'},
        {'text': '➕ Добавить устройство', 'callback_data': 'add'},
    ]]}


def _menu_text(view: dict) -> str:
    name = view.get('name', '?')
    count = len(view.get('devices', []))
    limit = _limit_label(view.get('deviceLimit'))
    return (
        f'🔐 *VPN-доступ* для *{name}*.\n'
        f'Устройства: {count} из {limit}.'
    )


def _devices_markup(view: dict) -> dict:
    rows = []
    for d in view.get('devices', []):
        rows.append([{
            'text': f'📱 {d.get("name", "?")}',
            'callback_data': f'dev:{d.get("id")}',
        }])
    rows.append([{'text': '‹ Назад', 'callback_data': 'menu'}])
    return {'inline_keyboard': rows}


def _devices_text(view: dict) -> str:
    devices = view.get('devices', [])
    if not devices:
        return 'У вас пока нет устройств. Нажмите «➕ Добавить устройство» в меню.'
    return f'📱 *Ваши устройства* ({len(devices)} шт.)\nВыберите устройство:'


def _find_device(view: dict, device_id: str) -> dict | None:
    for d in view.get('devices', []):
        if str(d.get('id')) == str(device_id):
            return d
    return None


def _device_card_text(device: dict) -> str:
    name = device.get('name', '?')
    parts = []
    if device.get('hasWg'):
        parts.append('WireGuard')
    if device.get('hasOvpn'):
        parts.append('OpenVPN')
    proto = ', '.join(parts) if parts else 'нет конфигов'
    return f'📱 *{name}*\nПротоколы: {proto}\n\nВыберите действие:'


def _device_card_markup(device_id: str) -> dict:
    return {'inline_keyboard': [
        [
            {'text': 'WireGuard', 'callback_data': f'wg:{device_id}'},
            {'text': 'OpenVPN', 'callback_data': f'ovpn:{device_id}'},
        ],
        [
            {'text': '🔄 Перевыпустить ключи', 'callback_data': f'reic:{device_id}'},
            {'text': '🗑 Удалить', 'callback_data': f'del:{device_id}'},
        ],
        [{'text': '‹ Назад', 'callback_data': 'devs'}],
    ]}


def _confirm_markup(yes_data: str, no_data: str) -> dict:
    return {'inline_keyboard': [[
        {'text': '✅ Да', 'callback_data': yes_data},
        {'text': '✖ Отмена', 'callback_data': no_data},
    ]]}


# ── Commands ───────────────────────────────────────────────────────────────────

ADMIN_START_TEXT = (
    '*anysda-vpn*\n\n'
    '/status — состояние системы\n'
    '/nodes  — RTT по exit-нодам\n'
    '/clients — клиенты'
)


async def _send_client_menu(chat_id: int, view: dict) -> None:
    await tg_send(chat_id, _menu_text(view), 'Markdown', _menu_markup())


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    # Админский чат — текущее админское приветствие, без изменений.
    if chat_id == CHAT_ID:
        await tg_reply(update, ADMIN_START_TEXT, 'Markdown')
        return

    payload = context.args[0] if context.args else None
    username = update.effective_user.username if update.effective_user else None

    if payload:
        # Диплинк-приглашение: t.me/bot?start=<пароль> → привязка чата.
        try:
            view = await api_link(chat_id, payload, username)
        except BotApiError as e:
            if e.status == 404:
                await tg_reply(
                    update,
                    'Ссылка недействительна. Запросите новое приглашение у администратора.',
                )
            else:
                await tg_reply(update, f'❌ {e.message}')
            return
        except Exception as e:
            log.exception('cmd_start link failed')
            await tg_reply(update, f'❌ Ошибка: {e}')
            return
        await tg_send(
            chat_id,
            f'✅ Доступ к VPN активирован для *{view.get("name", "?")}*.',
            'Markdown',
        )
        await _send_client_menu(chat_id, view)
        return

    # /start без payload — открыть меню, если чат уже привязан.
    try:
        view = await api_client(chat_id)
    except BotApiError as e:
        if e.status == 404:
            await tg_reply(
                update,
                'Чтобы пользоваться VPN, откройте ссылку-приглашение от администратора.',
            )
        else:
            await tg_reply(update, f'❌ {e.message}')
        return
    except Exception as e:
        log.exception('cmd_start client failed')
        await tg_reply(update, f'❌ Ошибка: {e}')
        return
    await _send_client_menu(chat_id, view)


async def build_status_text(header: str = '*Состояние системы*') -> str:
    data = await api_status()
    lines = [header + '\n']
    for n in data.get('nodes', []):
        net = (n.get('rxMbps') or 0) + (n.get('txMbps') or 0)
        lines.append(
            f"🖥 *{n.get('tag', '?').upper()}*: CPU {_pct(n.get('cpu'))}"
            f" | RAM {_pct(n.get('ram'))}"
            f" | ↕ {net:.1f} Mbps"
        )
    lines.append('')
    for o in data.get('outbounds', []):
        if o.get('name') == 'direct-ru':
            continue
        rtt = o.get('rttMs')
        icon = '🟢' if rtt else '🔴'
        rtt_txt = f'{rtt} ms' if rtt else 'нет связи'
        lines.append(f"{icon} `{_ob_label(o.get('name', '?'))}`: {rtt_txt}")
    return '\n'.join(lines)


def _admin_only(update: Update) -> bool:
    return update.effective_chat.id == CHAT_ID


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin_only(update):
        return
    try:
        await tg_reply(update, await build_status_text(), 'Markdown')
    except Exception as e:
        log.exception('cmd_status failed')
        await tg_reply(update, f'❌ Ошибка: {e}')


async def cmd_nodes(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin_only(update):
        return
    try:
        data = await api_status()
        lines = ['*Exit-ноды*\n']
        for o in data.get('outbounds', []):
            if o.get('name') == 'direct-ru':
                continue
            rtt = o.get('rttMs')
            icon = '🟢' if rtt else '🔴'
            rtt_txt = f'{rtt} ms' if rtt else 'нет связи'
            lines.append(f"{icon} `{_ob_label(o.get('name', '?'))}`: {rtt_txt}")
        await tg_reply(
            update,
            '\n'.join(lines) if len(lines) > 1 else 'Нет нод',
            'Markdown',
        )
    except Exception as e:
        log.exception('cmd_nodes failed')
        await tg_reply(update, f'❌ Ошибка: {e}')


async def cmd_clients(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin_only(update):
        return
    try:
        data = await api_status()
        clients = data.get('clients', [])
        lines = [f'*Клиенты* ({len(clients)} шт.)\n']
        for c in clients:
            icon = '✅' if c.get('status') == 'active' else '❄️'
            lines.append(f"{icon} `{c.get('name', '?')}`")
        await tg_reply(
            update,
            '\n'.join(lines) if clients else 'Нет клиентов',
            'Markdown',
        )
    except Exception as e:
        log.exception('cmd_clients failed')
        await tg_reply(update, f'❌ Ошибка: {e}')


# ── Client text messages ───────────────────────────────────────────────────────

async def on_text(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Текстовые сообщения от клиентов. Админский чат — игнор (там команды)."""
    chat_id = update.effective_chat.id
    if chat_id == CHAT_ID:
        return
    text = (update.message.text or '').strip() if update.message else ''

    # Чат ждёт название устройства — обрабатываем как имя.
    if chat_id in _awaiting_device_name:
        _awaiting_device_name.discard(chat_id)
        if not text:
            await tg_send(chat_id, 'Название не распознано. Откройте меню: /start')
            return
        try:
            view = await api_add_device(chat_id, text)
        except BotApiError as e:
            await tg_send(chat_id, f'❌ {e.message}')
            return
        except Exception as e:
            log.exception('add_device failed')
            await tg_send(chat_id, f'❌ Ошибка: {e}')
            return
        await tg_send(chat_id, f'✅ Устройство «{text}» добавлено.')
        await _send_client_menu(chat_id, view)
        return

    # Обычное текстовое сообщение от привязанного клиента — показать меню.
    try:
        view = await api_client(chat_id)
    except BotApiError as e:
        if e.status == 404:
            await tg_send(
                chat_id,
                'Чтобы пользоваться VPN, откройте ссылку-приглашение от администратора.',
            )
        else:
            await tg_send(chat_id, f'❌ {e.message}')
        return
    except Exception as e:
        log.exception('on_text client failed')
        await tg_send(chat_id, f'❌ Ошибка: {e}')
        return
    await _send_client_menu(chat_id, view)


# ── Client callback queries (инлайн-кнопки) ─────────────────────────────────────

async def on_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    chat_id = update.effective_chat.id
    callback_id = query.id
    message_id = query.message.message_id if query.message else None
    data = query.data or ''

    # Админский чат инлайн-кнопками не пользуется — просто гасим «часики».
    if chat_id == CHAT_ID:
        await tg_answer_callback(callback_id)
        return

    try:
        await _handle_callback(chat_id, message_id, callback_id, data)
    except BotApiError as e:
        await tg_answer_callback(callback_id, e.message, show_alert=True)
    except Exception as e:
        log.exception('callback failed: %s', data)
        await tg_answer_callback(callback_id, f'Ошибка: {e}', show_alert=True)


async def _handle_callback(chat_id: int, message_id: int | None,
                           callback_id: str, data: str) -> None:
    action, _, arg = data.partition(':')

    # Любое действие требует привязанного клиента.
    if action == 'menu':
        view = await api_client(chat_id)
        await tg_answer_callback(callback_id)
        if message_id:
            await tg_edit(chat_id, message_id, _menu_text(view), 'Markdown', _menu_markup())
        return

    if action == 'devs':
        view = await api_client(chat_id)
        await tg_answer_callback(callback_id)
        if message_id:
            await tg_edit(chat_id, message_id, _devices_text(view), 'Markdown',
                          _devices_markup(view))
        return

    if action == 'dev':
        view = await api_client(chat_id)
        device = _find_device(view, arg)
        if device is None:
            await tg_answer_callback(callback_id, 'Устройство не найдено', show_alert=True)
            if message_id:
                await tg_edit(chat_id, message_id, _devices_text(view), 'Markdown',
                              _devices_markup(view))
            return
        await tg_answer_callback(callback_id)
        if message_id:
            await tg_edit(chat_id, message_id, _device_card_text(device), 'Markdown',
                          _device_card_markup(arg))
        return

    if action == 'add':
        view = await api_client(chat_id)
        limit = view.get('deviceLimit')
        if limit is not None and len(view.get('devices', [])) >= limit:
            await tg_answer_callback(
                callback_id,
                'Достигнут лимит устройств. Обратитесь к администратору.',
                show_alert=True,
            )
            return
        _awaiting_device_name.add(chat_id)
        await tg_answer_callback(callback_id)
        await tg_send(
            chat_id,
            'Пришлите название устройства (например: Телефон, Ноутбук).',
        )
        return

    if action == 'wg':
        view = await api_client(chat_id)
        device = _find_device(view, arg)
        if device is None:
            await tg_answer_callback(callback_id, 'Устройство не найдено', show_alert=True)
            return
        await tg_answer_callback(callback_id, 'Готовлю конфиг WireGuard…')
        conf = await api_wg_config(chat_id, arg)
        config_name = device.get('configName') or device.get('name') or 'wireguard'
        await tg_send_qr(chat_id, conf, f'*{device.get("name", "?")}* — WireGuard')
        await tg_send_document(
            chat_id, f'{config_name}.conf', conf.encode('utf-8'),
            caption=f'*{device.get("name", "?")}* — WireGuard',
        )
        return

    if action == 'ovpn':
        view = await api_client(chat_id)
        device = _find_device(view, arg)
        if device is None:
            await tg_answer_callback(callback_id, 'Устройство не найдено', show_alert=True)
            return
        await tg_answer_callback(callback_id, 'Готовлю конфиг OpenVPN…')
        conf = await api_ovpn_config(chat_id, arg)
        config_name = device.get('configName') or device.get('name') or 'openvpn'
        await tg_send_document(
            chat_id, f'{config_name}.ovpn', conf.encode('utf-8'),
            caption=f'*{device.get("name", "?")}* — OpenVPN',
        )
        return

    if action == 'reic':
        # Запрос подтверждения перевыпуска.
        await tg_answer_callback(callback_id)
        if message_id:
            await tg_edit(
                chat_id, message_id,
                '🔄 *Перевыпустить ключи?*\n\n'
                'Старые конфиги перестанут работать — '
                'нужно будет выдать новые на все устройства.',
                'Markdown',
                _confirm_markup(f'reicyes:{arg}', f'dev:{arg}'),
            )
        return

    if action == 'reicyes':
        await tg_answer_callback(callback_id, 'Перевыпускаю ключи…')
        view = await api_reissue_device(chat_id, arg)
        device = _find_device(view, arg)
        if message_id and device is not None:
            await tg_edit(
                chat_id, message_id,
                f'✅ Ключи перевыпущены.\n\n{_device_card_text(device)}',
                'Markdown',
                _device_card_markup(arg),
            )
        elif message_id:
            await tg_edit(chat_id, message_id, _devices_text(view), 'Markdown',
                          _devices_markup(view))
        return

    if action == 'del':
        # Запрос подтверждения удаления.
        view = await api_client(chat_id)
        device = _find_device(view, arg)
        dev_name = device.get('name', '?') if device else '?'
        await tg_answer_callback(callback_id)
        if message_id:
            await tg_edit(
                chat_id, message_id,
                f'🗑 *Удалить устройство «{dev_name}»?*\n\n'
                'Конфиги этого устройства перестанут работать.',
                'Markdown',
                _confirm_markup(f'delyes:{arg}', f'dev:{arg}'),
            )
        return

    if action == 'delyes':
        await tg_answer_callback(callback_id, 'Удаляю устройство…')
        view = await api_delete_device(chat_id, arg)
        if message_id:
            await tg_edit(chat_id, message_id, _devices_text(view), 'Markdown',
                          _devices_markup(view))
        return

    # Неизвестное действие — просто гасим «часики».
    await tg_answer_callback(callback_id)


# ── Background monitor ──────────────────────────────────────────────────────────

# Алерты: проверки каждые MONITOR_INTERVAL сек, ALERT_HITS подряд = триггер.
# Снятие — когда метрика опускается ниже (порог - HYSTERESIS).
# По умолчанию: 30с * 2 = ~60с до CPU/RAM-алерта.
MONITOR_INTERVAL = int(os.environ.get('ALERT_INTERVAL_SEC', '30'))
ALERT_HITS       = int(os.environ.get('ALERT_HITS',         '2'))
CPU_HIGH = float(os.environ.get('ALERT_CPU_PCT', '80'))
RAM_HIGH = float(os.environ.get('ALERT_RAM_PCT', '80'))
HYSTERESIS = 10.0
# Нода считается «лежит», когда её метрики устарели больше чем на столько
# секунд (или их нет вовсе). По умолчанию 5 минут.
NODE_DOWN_SEC = int(os.environ.get('ALERT_NODE_DOWN_SEC', '300'))

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
                f'⚠️ *{tag.upper()}*: нагрузка на {label} выше '
                f'{threshold:.0f}{unit} ({value:.0f}{unit})',
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
            nodes = data.get('nodes', [])

            # Нода лежит: метрики устарели > NODE_DOWN_SEC (или их нет вовсе —
            # staleSec=None, VM уже не отдаёт серию). staleSec — реальный
            # возраст последнего сэмпла node_exporter. ALERT_HITS подряд —
            # отсекаем кратковременный рестарт VM, когда staleSec мигает в None.
            for n in nodes:
                tag = n.get('tag', '?')
                stale = n.get('staleSec')
                down = stale is None or stale >= NODE_DOWN_SEC
                if down:
                    _node_down[tag] = _node_down.get(tag, 0) + 1
                    if _node_down[tag] >= ALERT_HITS and tag not in _node_alerted:
                        _node_alerted.add(tag)
                        log.info('ALERT: нода %s лежит — отправляю алерт', tag)
                        await tg_send(
                            CHAT_ID,
                            f'🔴 *Нода {tag.upper()} лежит* — '
                            f'метрик нет больше {NODE_DOWN_SEC // 60} мин',
                            'Markdown',
                        )
                else:
                    if tag in _node_alerted:
                        _node_alerted.discard(tag)
                        log.info('нода %s восстановилась — отправляю алерт', tag)
                        await tg_send(
                            CHAT_ID,
                            f'🟢 *Нода {tag.upper()} восстановилась*',
                            'Markdown',
                        )
                    _node_down[tag] = 0

            # CPU / RAM на каждой ноде
            for n in nodes:
                tag = n.get('tag', '?')
                cpu = n.get('cpu')
                ram = n.get('ram')
                if isinstance(cpu, (int, float)):
                    await _check_load(tag, 'cpu', float(cpu), CPU_HIGH, 'CPU')
                if isinstance(ram, (int, float)):
                    await _check_load(tag, 'ram', float(ram), RAM_HIGH, 'RAM')
        except Exception as e:
            log.warning('monitor error: %s', e)


# ── HTTP server for Nuxt events ────────────────────────────────────────────────

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
    elif evt == 'client_send_wireguard':
        name = data.get('name', '?')
        conf = data.get('conf', '')
        if conf:
            # WireGuard: два сообщения — сначала QR как фото, затем .conf файлом.
            async def _send_wg():
                await tg_send_qr(CHAT_ID, conf, f'*{name}* — WireGuard')
                await tg_send_document(
                    CHAT_ID, f'{name}.conf', conf.encode('utf-8'),
                    caption=f'*{name}* — WireGuard',
                )
            asyncio.create_task(_send_wg())
    elif evt == 'client_send_openvpn':
        name = data.get('name', '?')
        conf = data.get('conf', '')
        if conf:
            # .ovpn carries inline certs — too large for a QR, send as a file.
            asyncio.create_task(tg_send_document(
                CHAT_ID, f'{name}.ovpn', conf.encode('utf-8'),
                caption=f'*{name}* — OpenVPN',
            ))
    elif evt == 'client_send_password':
        # Приглашение клиенту: бот шлёт в АДМИНСКИЙ чат два сообщения —
        # (1) пояснение админу, (2) готовое приветствие с диплинком, которое
        # админ пересылает клиенту.
        name = data.get('name', '?')
        password = data.get('password', '')
        if password:
            asyncio.create_task(_send_invite(name, password))
    elif evt == 'client_quota_raised':
        # Лимит устройств клиента повышен — уведомить сам клиентский чат.
        chat_id = data.get('chatId')
        device_limit = data.get('deviceLimit')
        if chat_id:
            if device_limit is None:
                msg = '📈 Ваш лимит устройств снят — теперь безлимит.'
            else:
                msg = f'📈 Ваш лимит устройств повышен до {device_limit}.'
            asyncio.create_task(tg_send(int(chat_id), msg))
    elif evt == 'deploy_done':
        asyncio.create_task(_announce_deploy())
    return web.Response(text='ok')


async def _send_invite(name: str, password: str) -> None:
    """Два сообщения в админский чат: пояснение + готовое приглашение клиенту."""
    await tg_send(
        CHAT_ID,
        f'📨 Приглашение для «{name}». Перешлите клиенту сообщение ниже ↓',
    )
    if BOT_USERNAME:
        deeplink = f'https://t.me/{BOT_USERNAME}?start={password}'
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
    await tg_send(CHAT_ID, invite)


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


# ── main ────────────────────────────────────────────────────────────────────────

async def _fetch_bot_username() -> None:
    """getMe → BOT_USERNAME, для построения диплинков-приглашений."""
    global BOT_USERNAME
    try:
        r = await _tg.get('/getMe', timeout=20)
        body = r.json()
        if body.get('ok'):
            BOT_USERNAME = body.get('result', {}).get('username')
            log.info('BOT_USERNAME=%s', BOT_USERNAME)
        else:
            log.warning('getMe не ok: %s', body)
    except Exception as e:
        log.warning('getMe failed: %s — диплинки будут с фолбэк-инструкцией', e)


async def main() -> None:
    # Telegram egress — через TG_PROXY (HTTP-CONNECT прокси sing-box →
    # foreign-best → экзит): RU-нода в Москве api.telegram.org напрямую не
    # достаёт. Используем HTTP-CONNECT, а не SOCKS5: httpx-socks давал
    # ложные PoolTimeout, тогда как CONNECT-туннели httpx пулит надёжно.
    # connection_pool_size + pool_timeout default'ы в HTTPXRequest очень
    # маленькие (1 и 1.0с) — при любой задержке handler-команд получаем
    # PoolTimeout. Поднимаем с запасом, чтобы команды + monitor_loop +
    # event_server жили вместе.
    poll_req = HTTPXRequest(connection_pool_size=4, read_timeout=40, proxy=TG_PROXY)
    send_req = HTTPXRequest(
        connection_pool_size=50,
        pool_timeout=20.0,
        connect_timeout=10.0,
        read_timeout=20.0,
        proxy=TG_PROXY,
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
    # Клиентское инлайн-меню + ввод названия устройства.
    tgapp.add_handler(CallbackQueryHandler(on_callback))
    tgapp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # username бота для диплинков-приглашений.
    await _fetch_bot_username()

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
        log.info('Bot started, polling Telegram via %s', TG_PROXY)
        await asyncio.Event().wait()  # run forever
        await tgapp.updater.stop()
        await tgapp.stop()

    await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
