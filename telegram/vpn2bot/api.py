"""Both HTTP sides of the bot: the Telegram Bot API through the exit proxy, and the panel."""
import asyncio
import json
import os
import re
import time

import httpx
from telegram import Bot, InputFile
from telegram.error import BadRequest
from telegram.request import HTTPXRequest

from . import config
from .config import log
from .texts import ERROR_FALLBACK, ERROR_TEXT

panel_http = httpx.AsyncClient(base_url=config.ANYSDA_URL, timeout=10.0, trust_env=False,
                               headers={'Authorization': f'Bearer {config.SECRET}'})


async def plain_if_unparsable(call, **kwargs):
    """Markdown that Telegram cannot parse goes again as plain text."""
    try:
        return await call(**kwargs)
    except BadRequest as e:
        if not kwargs.get('parse_mode') or "can't parse" not in e.message.lower():
            raise
        return await call(**{**kwargs, 'parse_mode': None})


async def send(chat_id: int, text: str, parse_mode: str | None = None,
               reply_markup: dict | None = None, message_id: int | None = None) -> None:
    message = {'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode, 'reply_markup': reply_markup}
    if not message_id:
        await plain_if_unparsable(bot.send_message, **message)
        return
    try:
        await plain_if_unparsable(bot.edit_message_text, message_id=message_id, **message)
    except BadRequest as e:
        if 'message is not modified' not in e.message.lower():
            raise


async def send_file(chat_id: int, kind: str, filename: str, content: bytes, caption: str) -> None:
    method = bot.send_photo if kind == 'photo' else bot.send_document
    await plain_if_unparsable(method, chat_id=chat_id, caption=caption, parse_mode='Markdown',
                              **{kind: InputFile(content, filename=filename)})


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


# Without a token the bot only answers /health and waits for the panel (see __main__).
bot = Bot(config.TOKEN, request=HTTPXRequest(connection_pool_size=20, connect_timeout=10.0, read_timeout=20.0,
                                            pool_timeout=10.0, proxy=config.TG_PROXY),
          get_updates_request=WatchedRequest(connection_pool_size=4, read_timeout=40, proxy=config.TG_PROXY),
          ) if config.TOKEN else None


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
