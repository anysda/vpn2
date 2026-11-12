"""Both HTTP sides of the bot: the Telegram Bot API through the exit proxy, and the panel."""
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

panel_http = httpx.AsyncClient(base_url=config.ANYSDA_URL, timeout=10.0, trust_env=False,
                               headers={'Authorization': f'Bearer {config.SECRET}'})
# Sends bypass the python-telegram-bot pool; getUpdates goes through WatchedRequest.
telegram_http = httpx.AsyncClient(
    base_url=f'https://api.telegram.org/bot{config.TOKEN}', proxy=config.TG_PROXY,
    timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))


def given(**fields) -> dict:
    return {key: value for key, value in fields.items() if value is not None}


async def telegram(method: str, payload: dict, files: dict | None = None) -> dict:
    """Bot API call raising on ok:false; Markdown that Telegram cannot parse goes again as plain text."""
    while True:
        if files is None:
            r = await telegram_http.post(f'/{method}', json=payload, timeout=20)
        else:
            r = await telegram_http.post(f'/{method}', data=payload, files=files, timeout=30)
        body = r.json()
        desc = str(body.get('description', ''))
        if body.get('ok') or (method == 'editMessageText' and 'message is not modified' in desc):
            return body
        if 'parse_mode' not in payload or "can't parse" not in desc:
            retry_after = body.get('parameters', {}).get('retry_after', 0)
            raise TelegramSendError(body.get('error_code', r.status_code),
                                    float(retry_after) if isinstance(retry_after, (int, float)) else 0)
        payload = {k: v for k, v in payload.items() if k != 'parse_mode'}


async def send(chat_id: int, text: str, parse_mode: str | None = None,
               reply_markup: dict | None = None, message_id: int | None = None) -> dict:
    payload = given(chat_id=chat_id, text=text, message_id=message_id or None,
                     parse_mode=parse_mode or None, reply_markup=reply_markup)
    return await telegram('editMessageText' if message_id else 'sendMessage', payload)


async def send_file(chat_id: int, kind: str, filename: str, content: bytes, caption: str) -> dict:
    mime = 'image/png' if kind == 'photo' else 'application/octet-stream'
    data = {'chat_id': str(chat_id), 'caption': caption, 'parse_mode': 'Markdown'}
    return await telegram(f'send{kind.capitalize()}', data, {kind: (filename, content, mime)})


class BotApiError(Exception):
    def __init__(self, status: int, message: str, code: str = ''):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


_CODE_RE = re.compile(r'[A-Za-z0-9_]{1,64}')


def _panel_error(r: httpx.Response) -> BotApiError:
    if r.status_code >= 500:
        return BotApiError(r.status_code, 'Панель временно недоступна. Попробуйте позже.')
    try:
        body = r.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    message = body.get('message') if isinstance(body.get('message'), str) else ''
    code = next((c for c in (body.get('statusMessage'), message)
                 if isinstance(c, str) and _CODE_RE.fullmatch(c)), '')
    if code in ERROR_TEXT:
        return BotApiError(r.status_code, ERROR_TEXT[code], code)
    if re.search(r'[А-Яа-яЁё]', message):
        return BotApiError(r.status_code, message, code)
    log.warning('панель ответила %s с кодом без текста: %s', r.status_code, code or '—')
    return BotApiError(r.status_code, ERROR_FALLBACK, code)


async def panel(method: str, path: str, body: dict | None = None) -> httpx.Response:
    r = await panel_http.request(method, path, json=body)
    if r.status_code >= 400:
        raise _panel_error(r)
    return r


def device_path(device_id: str, tail: str = '') -> str:
    # callback_data comes from the Telegram client: no ../ or ?chatId= into the panel path.
    if not re.fullmatch(r'[1-9][0-9]*', device_id):
        raise BotApiError(400, 'Некорректное устройство')
    return f'/api/bot/devices/{device_id}{tail}'


async def status() -> dict:
    r = await panel_http.get('/api/ops/bot-snapshot')
    r.raise_for_status()
    return r.json()


LAST_POLL_OK: float = time.monotonic()


class WatchedRequest(HTTPXRequest):
    """getUpdates transport: only an ok Bot API answer with an updates list counts as a live poll."""

    async def do_request(self, *args, **kwargs):
        global LAST_POLL_OK
        status, payload = result = await super().do_request(*args, **kwargs)
        try:
            body = json.loads(payload) if status == 200 else None
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get('ok') is True and isinstance(body.get('result'), list):
            LAST_POLL_OK = time.monotonic()
        return result


async def poll_watchdog() -> None:
    """A hung poller never recovers by itself: exit, docker --restart starts polling anew."""
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
