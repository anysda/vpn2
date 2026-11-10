import asyncio
import io
import json
import os
import time

import httpx
import qrcode
from telegram import Update
from telegram.request import HTTPXRequest

from . import config
from .config import log
from .outbox import TelegramSendError


# monotonic-время последнего успешного ответа Telegram на getUpdates.
# Пишет _mark_poll_ok, читают _poll_watchdog и /health.
LAST_POLL_OK: float = time.monotonic()


def _mark_poll_ok() -> None:
    global LAST_POLL_OK
    LAST_POLL_OK = time.monotonic()


class _WatchedRequest(HTTPXRequest):
    """HTTPXRequest под getUpdates, отмечающий каждый успешный ответ Telegram.

    Только успешный Bot API ответ с массивом updates подтверждает поллинг.
    Ошибка прокси или Telegram не должна удерживать /health в зелёном состоянии.
    """

    async def do_request(self, *args, **kwargs):
        result = await super().do_request(*args, **kwargs)
        status, payload = result
        if status == 200:
            try:
                body = json.loads(payload)
            except (ValueError, UnicodeError):
                body = None
            if (isinstance(body, dict) and body.get('ok') is True
                    and isinstance(body.get('result'), list)):
                _mark_poll_ok()
        return result


WATCHDOG_TICK_SEC = 30


async def poll_watchdog() -> None:
    """Уронить процесс, если getUpdates перестал отвечать.

    Из зависшего поллинга бот сам не выбирается (03-08-2026 висел 38 часов),
    поэтому единственное лечение — выход: docker --restart unless-stopped
    поднимет контейнер, и поллинг начнётся заново.
    """
    if config.POLL_STALL_SEC <= 0:
        log.info('poll watchdog выключен (TGBOT_POLL_STALL_SEC=0)')
        return
    while True:
        await asyncio.sleep(WATCHDOG_TICK_SEC)
        idle = time.monotonic() - LAST_POLL_OK
        if idle > config.POLL_STALL_SEC:
            log.error(
                'poller завис: %.0fс без ответа на getUpdates (лимит %dс) — '
                'выхожу, docker перезапустит контейнер', idle, config.POLL_STALL_SEC,
            )
            os._exit(1)


# Local client — for Nuxt API on 127.0.0.1 (direct, no proxy)
_local = httpx.AsyncClient(base_url=config.ANYSDA_URL, timeout=10.0, trust_env=False)

# Отдельный httpx-клиент для send-методов Telegram — в обход пула
# python-telegram-bot (с ним ловили PoolTimeout). polling getUpdates идёт
# через свой HTTPXRequest в Application. Оба ходят через TG_PROXY → экзит.
_tg = httpx.AsyncClient(
    base_url=f'https://api.telegram.org/bot{config.TOKEN}',
    proxy=config.TG_PROXY,
    timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)


# ── Telegram API helpers (raw httpx через TG_PROXY) ────────────────────────────


async def _tg_post(method: str, payload: dict, files: dict | None = None,
                   timeout: float = 20) -> dict:
    """Вызов Bot API с проверкой ok. Если не разобралась разметка, повтор
    без неё: пусть со звёздочками, но сообщение дойдёт."""
    for attempt in range(2):
        if files is None:
            r = await _tg.post(f'/{method}', json=payload, timeout=timeout)
        else:
            r = await _tg.post(f'/{method}', data=payload, files=files, timeout=timeout)
        body = r.json()
        if body.get('ok'):
            return body
        desc = str(body.get('description', ''))
        if method == 'editMessageText' and 'message is not modified' in desc:
            return body
        if attempt or 'parse_mode' not in payload or "can't parse" not in desc:
            retry_after = body.get('parameters', {}).get('retry_after', 0)
            raise TelegramSendError(body.get('error_code', r.status_code),
                                    float(retry_after) if isinstance(retry_after, (int, float)) else 0)
        payload = {k: v for k, v in payload.items() if k != 'parse_mode'}
    return body


async def tg_send(chat_id: int, text: str, parse_mode: str | None = None,
                   reply_markup: dict | None = None) -> dict:
    payload: dict = {'chat_id': chat_id, 'text': text}
    if parse_mode:
        payload['parse_mode'] = parse_mode
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    return await _tg_post('sendMessage', payload)


async def tg_edit(chat_id: int, message_id: int, text: str,
                  parse_mode: str | None = None,
                  reply_markup: dict | None = None) -> dict:
    """editMessageText — для перерисовки инлайн-меню без новых сообщений."""
    payload: dict = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
    if parse_mode:
        payload['parse_mode'] = parse_mode
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    return await _tg_post('editMessageText', payload)


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
    """Render a config string into a PNG QR — black on white, big margin so phones lock onto it.

    ecl L as in the panel keeps the version low. Telegram downsizes photos over
    1280 px, so the module size is the largest that fits: 10 px for short
    configs, 9 px for the worst split config (version 27), 6 px at version 40.
    """
    ecl = qrcode.constants.ERROR_CORRECT_L
    probe = qrcode.QRCode(error_correction=ecl, border=4)
    probe.add_data(data)
    probe.make(fit=True)
    box = max(1, min(10, 1280 // (probe.modules_count + 8)))
    img = qrcode.make(data, error_correction=ecl, box_size=box, border=4)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


async def tg_send_qr(chat_id: int, payload: str, caption: str) -> dict:
    """Send a QR PNG with caption as photo — phones can scan straight from chat."""
    png = _make_qr_png(payload)
    files = {'photo': ('qr.png', png, 'image/png')}
    data = {'chat_id': str(chat_id), 'caption': caption, 'parse_mode': 'Markdown'}
    return await _tg_post('sendPhoto', data, files, timeout=30)


async def tg_send_document(chat_id: int, filename: str, content: bytes, caption: str = '') -> dict:
    """Send raw bytes as a Telegram document (used for .conf delivery)."""
    files = {'document': (filename, content, 'application/octet-stream')}
    data: dict = {'chat_id': str(chat_id)}
    if caption:
        data['caption'] = caption
        data['parse_mode'] = 'Markdown'
    return await _tg_post('sendDocument', data, files, timeout=30)


async def tg_reply(update: Update, text: str, parse_mode: str | None = None,
                   reply_markup: dict | None = None) -> dict:
    return await tg_send(update.effective_chat.id, text, parse_mode, reply_markup)
