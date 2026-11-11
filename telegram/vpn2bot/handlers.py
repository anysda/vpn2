"""Команды, кнопки и текст: одна таблица маршрутов и один разбор входящего."""
import re
from dataclasses import dataclass
from datetime import datetime

from . import api, config, events, texts
from .api import BotApiError
from .config import log

ADMIN, CLIENT = 'admin', 'client'
TEXT = 'text'

# Чаты, от которых ждём название устройства после «Добавить устройство».
awaiting_device_name: set[int] = set()
# Чего ждём текстом в админском чате (он один): ('anew', '') — имя нового
# клиента, ('aexp', '<id>') — срок действия клиента <id>.
admin_await: tuple | None = None


@dataclass
class Ask:
    """Одно входящее: кто, что нажал или написал, и куда отвечать."""
    chat_id: int
    action: str
    arg: str
    username: str | None = None
    callback_id: str | None = None
    message_id: int | None = None

    async def say(self, text: str, parse_mode: str | None = None, markup: dict | None = None):
        await api.send(self.chat_id, text, parse_mode, markup)

    async def show(self, text: str, markup: dict):
        """Перерисовать сообщение с нажатой кнопкой; без кнопки - прислать новое."""
        await api.send(self.chat_id, text, 'Markdown', markup, self.message_id)

    async def answer(self, text: str = '', alert: bool = False) -> dict:
        return await api.answer(self.callback_id, text, alert)

    async def json(self, method: str, path: str, body: dict | None = None):
        return (await api.panel(method, path, body)).json()


# ── Клиент ─────────────────────────────────────────────────────────────────────

async def client_view(ask: Ask) -> dict:
    return await ask.json('GET', f'/api/bot/client?chatId={ask.chat_id}')


async def show_devices(ask: Ask, view: dict) -> None:
    await ask.show(texts.devices(view), texts.devices_markup(view))


async def client_start(ask: Ask) -> None:
    if ask.arg:
        # Диплинк-приглашение t.me/bot?start=<пароль> привязывает чат к клиенту.
        body = {'password': ask.arg, 'chatId': ask.chat_id}
        if ask.username:
            body['username'] = ask.username
        try:
            view = await ask.json('POST', '/api/bot/link', body)
        except BotApiError as e:
            if e.status != 404:
                raise
            await ask.say(texts.BAD_INVITE)
            return
        await ask.say(texts.LINKED.format(texts.md_bold(view.get('name', '?'))), 'Markdown')
    else:
        view = await client_view(ask)
    await ask.say(texts.menu(view), 'Markdown', texts.REPLY_KB)


async def client_text(ask: Ask) -> None:
    naming = ask.chat_id in awaiting_device_name
    awaiting_device_name.discard(ask.chat_id)
    # Кнопки навигации важнее ожидания названия.
    if naming and ask.arg not in (texts.NAV_DEVICES, texts.NAV_ADD):
        if not ask.arg:
            await ask.say(texts.NO_DEVICE_NAME)
            return
        view = await ask.json('POST', '/api/bot/devices', {'chatId': ask.chat_id, 'name': ask.arg})
        await ask.say(texts.DEVICE_ADDED.format(ask.arg))
    else:
        view = await client_view(ask)
        devices = view.get('devices', [])
        if ask.arg == texts.NAV_DEVICES:
            await ask.say(texts.devices(view), 'Markdown',
                          texts.devices_markup(view) if devices else None)
            return
        if ask.arg == texts.NAV_ADD:
            limit = view.get('deviceLimit')
            if limit is not None and len(devices) >= limit:
                await ask.say(texts.ERROR_TEXT['device_limit'])
                return
            awaiting_device_name.add(ask.chat_id)
            await ask.say(texts.ASK_DEVICE_NAME)
            return
    await ask.say(texts.menu(view), 'Markdown', texts.REPLY_KB)


async def device_list(ask: Ask) -> None:
    view = await client_view(ask)
    await ask.answer()
    await show_devices(ask, view)


async def device_card(ask: Ask) -> None:
    view = await client_view(ask)
    device = texts.find_device(view, ask.arg)
    if device is None:
        await ask.answer(texts.NO_DEVICE, alert=True)
        await show_devices(ask, view)
        return
    await ask.answer()
    await ask.show(texts.device_card(device), texts.device_card_markup(ask.arg))


async def device_config(ask: Ask) -> None:
    """Кнопки wg и ovpn: конфиг файлом, для WireGuard перед ним QR."""
    device = texts.find_device(await client_view(ask), ask.arg)
    if device is None:
        await ask.answer(texts.NO_DEVICE, alert=True)
        return
    title, default_name, ext = texts.CONFIGS[ask.action]
    await ask.answer(texts.PREPARING.format(title))
    base, query = api.device_path(ask.arg), f'?chatId={ask.chat_id}'
    conf = (await api.panel('GET', f'{base}/{ask.action}-config{query}')).text
    caption = f'{texts.md_bold(device.get("name", "?"))} — {title}'
    if ask.action == 'wg':
        # QR того же конфига рисует панель: один генератор с её SVG.
        qr = (await api.panel('GET', f'{base}/wg-qrcode.png{query}')).content
        await api.send_file(ask.chat_id, 'photo', 'qr.png', qr, caption)
    name = device.get('configName') or device.get('name') or default_name
    await api.send_file(ask.chat_id, 'document', f'{name}.{ext}', conf.encode('utf-8'), caption)


async def reissue_ask(ask: Ask) -> None:
    await ask.answer()
    await ask.show(texts.REISSUE_CONFIRM,
                   texts.confirm_markup(f'reicyes:{ask.arg}', f'dev:{ask.arg}'))


async def reissue(ask: Ask) -> None:
    await ask.answer(texts.REISSUING)
    view = await ask.json('POST', api.device_path(ask.arg, '/reissue'), {'chatId': ask.chat_id})
    device = texts.find_device(view, ask.arg)
    if device is None:
        await show_devices(ask, view)
        return
    await ask.show(texts.REISSUE_STARTED.format(texts.device_card(device)),
                   texts.device_card_markup(ask.arg))


async def delete_device_ask(ask: Ask) -> None:
    device = texts.find_device(await client_view(ask), ask.arg) or {}
    await ask.answer()
    question = texts.DELETE_DEVICE_ASK.format(device.get('name', '?'))
    await ask.show(texts.DELETE_DEVICE.format(texts.md_bold(question)),
                   texts.confirm_markup(f'delyes:{ask.arg}', f'dev:{ask.arg}'))


async def delete_device(ask: Ask) -> None:
    await ask.answer(texts.DELETING)
    view = await ask.json('DELETE', f'{api.device_path(ask.arg)}?chatId={ask.chat_id}')
    await show_devices(ask, view)


# ── Администратор ──────────────────────────────────────────────────────────────

CLIENTS = '/api/bot/admin/clients'


async def admin_start(ask: Ask) -> None:
    await ask.say(texts.ADMIN_START, 'Markdown')


async def status(ask: Ask) -> None:
    await ask.say(texts.status(await api.status()), 'Markdown')


async def nodes(ask: Ask) -> None:
    await ask.say(texts.nodes(await api.status()), 'Markdown')


async def client_list(ask: Ask) -> None:
    items = await ask.json('GET', CLIENTS)
    await ask.show(texts.CLIENT_LIST.format(len(items)), texts.clients_markup(items))


async def client_card(ask: Ask, client_id: str) -> None:
    c = await ask.json('GET', f'{CLIENTS}/{client_id}')
    await ask.show(texts.client_card(c), texts.client_card_markup(c))


async def open_clients(ask: Ask) -> None:
    await ask.answer()
    await (client_card(ask, ask.arg) if ask.arg else client_list(ask))


async def ask_input(ask: Ask) -> None:
    """Кнопки «Создать клиента» и «Срок»: дальше ждём текст."""
    global admin_await
    admin_await = (ask.action, ask.arg)
    await ask.answer()
    if ask.action == 'anew':
        await ask.say(texts.ASK_CLIENT_NAME)
    else:
        await ask.say(texts.ASK_EXPIRY, 'Markdown')


async def freeze(ask: Ask) -> None:
    c = await ask.json('GET', f'{CLIENTS}/{ask.arg}')
    frozen = not c.get('frozenManual')
    await ask.json('PATCH', f'{CLIENTS}/{ask.arg}', {'frozenManual': frozen})
    await ask.answer(texts.ACCESS_OFF if frozen else texts.ACCESS_ON)
    await client_card(ask, ask.arg)


async def retry(ask: Ask) -> None:
    c = await ask.json('GET', f'{CLIENTS}/{ask.arg}')
    operation_id = (c.get('operation') or {}).get('id', '')
    if not re.fullmatch(r'[a-f0-9-]{36}', operation_id):
        await ask.answer(texts.NO_OPERATION, alert=True)
        return
    await api.panel('POST', f'/api/bot/admin/access-operations/{operation_id}/retry')
    await ask.answer(texts.RETRY_STARTED)
    await client_card(ask, ask.arg)


async def change_limit(ask: Ask) -> None:
    c = await ask.json('GET', f'{CLIENTS}/{ask.arg}')
    limit = c.get('deviceLimit')
    if ask.action == 'alm+':
        if limit is None:
            await ask.answer(texts.LIMIT_UNLIMITED)
            return
        limit += 1
    else:
        # С безлимита — на число устройств (не меньше 1). Ниже числа устройств
        # панель не пустит сама: вернёт 409.
        limit = max(c.get('deviceCount', 0), 1) if limit is None else limit - 1
    await ask.json('PATCH', f'{CLIENTS}/{ask.arg}', {'deviceLimit': limit})
    await ask.answer(texts.LIMIT_SET.format(limit))
    await client_card(ask, ask.arg)


async def invite(ask: Ask) -> None:
    c = await ask.json('GET', f'{CLIENTS}/{ask.arg}')
    if not c.get('password', ''):
        await ask.answer(texts.NO_PASSWORD, alert=True)
        return
    await ask.answer(texts.SENDING_INVITE)
    await events.send_invite(c)


async def delete_client_ask(ask: Ask) -> None:
    c = await ask.json('GET', f'{CLIENTS}/{ask.arg}')
    await ask.answer()
    question = texts.DELETE_CLIENT_ASK.format(c.get('name', '?'))
    await ask.show(texts.DELETE_CLIENT.format(texts.md_bold(question)),
                   texts.confirm_markup(f'adyes:{ask.arg}', f'acl:{ask.arg}'))


async def delete_client(ask: Ask) -> None:
    await ask.json('DELETE', f'{CLIENTS}/{ask.arg}')
    await ask.answer(texts.ACCESS_OFF)
    await client_list(ask)


async def admin_text(ask: Ask) -> None:
    """Текст в админском чате нужен, только пока ждём имя клиента или срок."""
    global admin_await
    if admin_await is None:
        return
    kind, client_id = admin_await
    if kind == 'anew':
        if not ask.arg:
            await ask.say(texts.NO_CLIENT_NAME)
            return
        admin_await = None
        client_id = str((await ask.json('POST', CLIENTS, {'name': ask.arg})).get('id'))
        await ask.say(texts.CLIENT_CREATED.format(ask.arg))
    else:
        expires = None
        if ask.arg.lower() not in texts.NEVER_EXPIRES:
            try:
                day = datetime.strptime(ask.arg, '%d.%m.%Y')
            except ValueError:
                await ask.say(texts.BAD_EXPIRY, 'Markdown')
                return
            expires = day.strftime('%Y-%m-%dT00:00:00.000Z')
        admin_await = None
        await ask.json('PATCH', f'{CLIENTS}/{client_id}', {'expiresAt': expires})
        await ask.say(texts.EXPIRY_CLEARED if expires is None else texts.EXPIRY_SET)
    await client_card(ask, client_id)


# ── Маршруты ───────────────────────────────────────────────────────────────────

# (кому доступно, команда | кнопка | TEXT) → обработчик. Чего в таблице нет,
# того этой роли нельзя: команда молча пропускается, кнопка просто гаснет.
ROUTES = {
    (CLIENT, '/start'): client_start,
    (CLIENT, '/help'): client_start,
    (CLIENT, TEXT): client_text,
    (CLIENT, 'devs'): device_list,
    (CLIENT, 'dev'): device_card,
    (CLIENT, 'wg'): device_config,
    (CLIENT, 'ovpn'): device_config,
    (CLIENT, 'reic'): reissue_ask,
    (CLIENT, 'reicyes'): reissue,
    (CLIENT, 'del'): delete_device_ask,
    (CLIENT, 'delyes'): delete_device,
    (ADMIN, '/start'): admin_start,
    (ADMIN, '/help'): admin_start,
    (ADMIN, '/status'): status,
    (ADMIN, '/nodes'): nodes,
    (ADMIN, '/clients'): client_list,
    (ADMIN, TEXT): admin_text,
    (ADMIN, 'acl'): open_clients,
    (ADMIN, 'anew'): ask_input,
    (ADMIN, 'afz'): freeze,
    (ADMIN, 'aretry'): retry,
    (ADMIN, 'alm+'): change_limit,
    (ADMIN, 'alm-'): change_limit,
    (ADMIN, 'aexp'): ask_input,
    (ADMIN, 'ainv'): invite,
    (ADMIN, 'adel'): delete_client_ask,
    (ADMIN, 'adyes'): delete_client,
}
COMMANDS = sorted({name[1:] for _, name in ROUTES if name.startswith('/')})


async def allowed(update) -> bool:
    """Личный чат удостоверяет владельца сам; в группе слушаем только её админов."""
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or user.is_bot:
        return False
    if chat.type == 'private':
        return user.id == chat.id
    if chat.id != config.CHAT_ID or chat.type not in ('group', 'supergroup'):
        return False
    # Анонимные сообщения от имени группы не удостоверяют отправителя.
    if update.message and update.message.sender_chat:
        return False
    try:
        member = await api.telegram('getChatMember', {'chat_id': chat.id, 'user_id': user.id})
        return member.get('result', {}).get('status') in ('creator', 'administrator')
    except Exception:
        log.warning('Не удалось проверить администратора Telegram-чата')
        return False


async def dispatch(update, _context) -> None:
    """Единая точка входа для команд, кнопок и текста."""
    if not await allowed(update):
        return
    chat_id, query = update.effective_chat.id, update.callback_query
    role = ADMIN if chat_id == config.CHAT_ID else CLIENT
    ask = Ask(chat_id, TEXT, '', update.effective_user.username)
    if query:
        ask.action, _, ask.arg = (query.data or '').partition(':')
        ask.callback_id = query.id
        ask.message_id = query.message.message_id if query.message else None
    else:
        message = update.effective_message
        words = ((message.text or '') if message else '').split()
        command = words[0].partition('@')[0].lower() if words else ''
        if command[1:] in COMMANDS and command.startswith('/'):
            ask.action, ask.arg = command, words[1] if len(words) > 1 else ''
        elif update.message:
            # Правка старого сообщения — не новый ввод: её текст не читаем.
            ask.arg = (update.message.text or '').strip()
    handler = ROUTES.get((role, ask.action))
    if handler is None:
        if query:
            await ask.answer()
        return
    try:
        await handler(ask)
    except Exception as e:
        await report_failure(ask, role, e)


async def report_failure(ask: Ask, role: str, error: Exception) -> None:
    """Один ответ на любую ошибку: отказ панели её словами, прочее - общей фразой."""
    if isinstance(error, BotApiError):
        message = error.message
    else:
        log.error('%s %s failed (%s)', role, ask.action, type(error).__name__)
        message = texts.UNEXPECTED_ERROR
    # Панель не знает этот чат: его не привязывали или отвязали посреди запроса.
    if (role == CLIENT and isinstance(error, BotApiError) and error.status == 404
            and 'not_linked' in (error.code, error.message)):
        # Сообщением: кнопку уже могли погасить «Готовлю конфиг…», второй ответ не дойдёт.
        if ask.callback_id:
            await ask.answer()
        await ask.say(texts.no_access())
    elif not ask.callback_id:
        await ask.say(f'❌ {message}')
    else:
        answered = await ask.answer(message, alert=True)
        if role == CLIENT and not answered.get('ok'):
            await ask.say(f'❌ {message}')
