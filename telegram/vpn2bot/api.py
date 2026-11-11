"""Обе HTTP-стороны бота: Telegram Bot API (через прокси экзита) и панель."""
import asyncio
import json
import os
import re
import time

import httpx
from telegram.request import HTTPXRequest

from . import config
from .config import log
from .outbox import TelegramSendError
from .texts import ERROR_FALLBACK, ERROR_TEXT

# Панель на 127.0.0.1: напрямую, без прокси.
panel_http = httpx.AsyncClient(base_url=config.ANYSDA_URL, timeout=10.0, trust_env=False,
                               headers={'Authorization': f'Bearer {config.SECRET}'})
# Отдельный httpx-клиент для send-методов Telegram — в обход пула
# python-telegram-bot (с ним ловили PoolTimeout). Поллинг getUpdates идёт через
# свой WatchedRequest. Оба ходят через TG_PROXY → экзит.
telegram_http = httpx.AsyncClient(
    base_url=f'https://api.telegram.org/bot{config.TOKEN}',
    proxy=config.TG_PROXY,
    timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)


async def telegram(method: str, payload: dict, files: dict | None = None) -> dict:
    """Вызов Bot API с проверкой ok. Если не разобралась разметка, повтор
    без неё: пусть со звёздочками, но сообщение дойдёт."""
    for attempt in range(2):
        if files is None:
            r = await telegram_http.post(f'/{method}', json=payload, timeout=20)
        else:
            r = await telegram_http.post(f'/{method}', data=payload, files=files, timeout=30)
        body = r.json()
        desc = str(body.get('description', ''))
        if body.get('ok') or (method == 'editMessageText' and 'message is not modified' in desc):
            return body
        if attempt or 'parse_mode' not in payload or "can't parse" not in desc:
            retry_after = body.get('parameters', {}).get('retry_after', 0)
            raise TelegramSendError(body.get('error_code', r.status_code),
                                    float(retry_after) if isinstance(retry_after, (int, float)) else 0)
        payload = {k: v for k, v in payload.items() if k != 'parse_mode'}
    return body


async def send(chat_id: int, text: str, parse_mode: str | None = None,
               reply_markup: dict | None = None, message_id: int | None = None) -> dict:
    """Новое сообщение; с message_id - перерисовать уже отправленное на месте."""
    payload: dict = {'chat_id': chat_id, 'text': text}
    if message_id:
        payload['message_id'] = message_id
    if parse_mode:
        payload['parse_mode'] = parse_mode
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    return await telegram('editMessageText' if message_id else 'sendMessage', payload)


async def answer(callback_id: str, text: str = '', alert: bool = False) -> dict:
    """answerCallbackQuery — гасит «часики» на нажатой инлайн-кнопке."""
    payload: dict = {'callback_query_id': callback_id}
    if text:
        payload['text'] = text
    if alert:
        payload['show_alert'] = True
    r = await telegram_http.post('/answerCallbackQuery', json=payload, timeout=20)
    return r.json()


async def send_file(chat_id: int, kind: str, filename: str, content: bytes, caption: str) -> dict:
    """Файл с подписью: kind 'photo' (QR, телефон сканирует прямо из чата) или 'document'."""
    mime = 'image/png' if kind == 'photo' else 'application/octet-stream'
    data = {'chat_id': str(chat_id), 'caption': caption, 'parse_mode': 'Markdown'}
    return await telegram(f'send{kind.capitalize()}', data, {kind: (filename, content, mime)})


class BotApiError(Exception):
    """Отказ /api/bot/*: message - текст для человека, code - машинный код панели."""

    def __init__(self, status: int, message: str, code: str = ''):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


_CODE_RE = re.compile(r'[A-Za-z0-9_]{1,64}')


def _panel_error(r: httpx.Response) -> BotApiError:
    """Из ответа панели — текст для человека; машинный код только в журнал."""
    if r.status_code >= 500:
        return BotApiError(r.status_code, 'Панель временно недоступна. Попробуйте позже.')
    try:
        body = r.json()
    except Exception:
        body = None
    body = body if isinstance(body, dict) else {}
    code = body.get('statusMessage')
    code = code if isinstance(code, str) and _CODE_RE.fullmatch(code) else ''
    message = body.get('message')
    message = message if isinstance(message, str) else ''
    code = code or (message if _CODE_RE.fullmatch(message) else '')
    if code in ERROR_TEXT:
        return BotApiError(r.status_code, ERROR_TEXT[code], code)
    # Текст, который обработчик панели написал сам («Клиент удаляется»).
    if re.search(r'[А-Яа-яЁё]', message):
        return BotApiError(r.status_code, message, code)
    log.warning('панель ответила %s с кодом без текста: %s', r.status_code, code or '—')
    return BotApiError(r.status_code, ERROR_FALLBACK, code)


async def panel(method: str, path: str, body: dict | None = None) -> httpx.Response:
    """Запрос к /api/bot/* панели; отказ становится BotApiError."""
    r = await panel_http.request(method, path, json=body)
    if r.status_code >= 400:
        raise _panel_error(r)
    return r


def device_path(device_id: str, tail: str = '') -> str:
    # callback_data задаёт клиент Telegram: запрещаем path/query/fragment,
    # иначе ../admin/... меняет маршрут, а ?chatId=... подменяет владельца.
    if not re.fullmatch(r'[1-9][0-9]*', device_id):
        raise BotApiError(400, 'Некорректное устройство')
    return f'/api/bot/devices/{device_id}{tail}'


async def status() -> dict:
    """Снимок узлов и выходов для /status и монитора."""
    r = await panel_http.get('/api/ops/bot-snapshot')
    r.raise_for_status()
    return r.json()


# monotonic-время последнего успешного ответа Telegram на getUpdates.
# Пишет WatchedRequest, читают poll_watchdog и /health.
LAST_POLL_OK: float = time.monotonic()


class WatchedRequest(HTTPXRequest):
    """HTTPXRequest под getUpdates, отмечающий каждый успешный ответ Telegram.

    Только успешный Bot API ответ с массивом updates подтверждает поллинг.
    Ошибка прокси или Telegram не должна удерживать /health в зелёном состоянии.
    """

    async def do_request(self, *args, **kwargs):
        global LAST_POLL_OK
        status, payload = result = await super().do_request(*args, **kwargs)
        if status == 200:
            try:
                body = json.loads(payload)
            except (ValueError, UnicodeError):
                body = None
            if (isinstance(body, dict) and body.get('ok') is True
                    and isinstance(body.get('result'), list)):
                LAST_POLL_OK = time.monotonic()
        return result


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
        await asyncio.sleep(30)
        idle = time.monotonic() - LAST_POLL_OK
        if idle > config.POLL_STALL_SEC:
            log.error('poller завис: %.0fс без ответа на getUpdates (лимит %dс) — '
                      'выхожу, docker перезапустит контейнер', idle, config.POLL_STALL_SEC)
            os._exit(1)
