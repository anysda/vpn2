import re
from contextlib import suppress

import httpx

from . import config, transport
from .config import log
from .texts import ERROR_FALLBACK, ERROR_TEXT


async def api_status() -> dict:
    headers = {'Authorization': f'Bearer {config.SECRET}'} if config.SECRET else {}
    r = await transport._local.get('/api/ops/bot-snapshot', headers=headers)
    r.raise_for_status()
    return r.json()


class BotApiError(Exception):
    """Ошибка вызова /api/bot/* — message человекочитаемый, для показа клиенту."""

    def __init__(self, status: int, message: str, code: str = ''):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code  # statusMessage панели: машинный код вроде not_linked


def _bot_headers() -> dict:
    return {'Authorization': f'Bearer {config.SECRET}'} if config.SECRET else {}


_CODE_RE = re.compile(r'[A-Za-z0-9_]{1,64}')
_CYRILLIC_RE = re.compile(r'[А-Яа-яЁё]')


def _extract_error(r: httpx.Response) -> BotApiError:
    """Из 4xx-ответа Nuxt — текст для человека; машинный код только в журнал."""
    if r.status_code >= 500:
        return BotApiError(r.status_code, 'Панель временно недоступна. Попробуйте позже.')
    body = None
    with suppress(Exception):
        body = r.json()
    body = body if isinstance(body, dict) else {}
    code = body.get('statusMessage')
    code = code if isinstance(code, str) and _CODE_RE.fullmatch(code) else ''
    message = body.get('message')
    message = message if isinstance(message, str) else ''
    code = code or (message if _CODE_RE.fullmatch(message) else '')
    if code in ERROR_TEXT:
        return BotApiError(r.status_code, ERROR_TEXT[code], code)
    # Текст, который обработчик панели написал сам («Клиент удаляется»).
    if _CYRILLIC_RE.search(message):
        return BotApiError(r.status_code, message, code)
    log.warning('панель ответила %s с кодом без текста: %s', r.status_code, code or '—')
    return BotApiError(r.status_code, ERROR_FALLBACK, code)


async def _bot_request(method: str, path: str, *, json_body: dict | None = None) -> httpx.Response:
    r = await transport._local.request(method, path, headers=_bot_headers(), json=json_body)
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


def _device_path(device_id: str) -> str:
    # callback_data задаёт клиент Telegram: запрещаем path/query/fragment,
    # иначе ../admin/... меняет маршрут, а ?chatId=... подменяет владельца.
    if not re.fullmatch(r'[1-9][0-9]*', device_id):
        raise BotApiError(400, 'Некорректное устройство')
    return f'/api/bot/devices/{device_id}'


async def api_delete_device(chat_id: int, device_id: str) -> dict:
    r = await _bot_request('DELETE', f'{_device_path(device_id)}?chatId={chat_id}')
    return r.json()


async def api_reissue_device(chat_id: int, device_id: str) -> dict:
    r = await _bot_request('POST', f'{_device_path(device_id)}/reissue',
                           json_body={'chatId': chat_id})
    return r.json()


async def api_wg_config(chat_id: int, device_id: str) -> str:
    r = await _bot_request('GET', f'{_device_path(device_id)}/wg-config?chatId={chat_id}')
    return r.text


# QR того же конфига рисует панель: один генератор с её SVG.
async def api_wg_qr_png(chat_id: int, device_id: str) -> bytes:
    r = await _bot_request('GET', f'{_device_path(device_id)}/wg-qrcode.png?chatId={chat_id}')
    return r.content


async def api_ovpn_config(chat_id: int, device_id: str) -> str:
    r = await _bot_request('GET', f'{_device_path(device_id)}/ovpn-config?chatId={chat_id}')
    return r.text


# ── Admin client API (Nuxt /api/bot/admin/clients) ─────────────────────────────

async def api_admin_clients() -> list[dict]:
    r = await _bot_request('GET', '/api/bot/admin/clients')
    return r.json()


async def api_admin_client(client_id: str) -> dict:
    r = await _bot_request('GET', f'/api/bot/admin/clients/{client_id}')
    return r.json()


async def api_admin_create_client(name: str) -> dict:
    r = await _bot_request('POST', '/api/bot/admin/clients', json_body={'name': name})
    return r.json()


async def api_admin_patch_client(client_id: str, patch: dict) -> dict:
    r = await _bot_request('PATCH', f'/api/bot/admin/clients/{client_id}', json_body=patch)
    return r.json()


async def api_admin_delete_client(client_id: str) -> dict:
    r = await _bot_request('DELETE', f'/api/bot/admin/clients/{client_id}')
    return r.json()
