from telegram import Update
from telegram.ext import ContextTypes

from . import admin, config, panel, transport
from .config import log
from .panel import BotApiError
from .texts import (
    ADMIN_START_TEXT,
    ERROR_TEXT,
    NAV_ADD,
    NAV_DEVICES,
    REPLY_KB,
    UNEXPECTED_ERROR,
    _confirm_markup,
    _device_card_markup,
    _device_card_text,
    _devices_markup,
    _devices_text,
    _find_device,
    _menu_text,
    _no_access_text,
    md_bold,
)


# Чаты, ожидающие ввода названия устройства (после нажатия «Добавить устройство»).
# Простое in-memory состояние — переживать рестарт не нужно.
_awaiting_device_name: set[int] = set()


async def _send_client_menu(chat_id: int, view: dict) -> None:
    """Меню клиента — текст + постоянная reply-клавиатура навигации."""
    await transport.tg_send(chat_id, _menu_text(view), 'Markdown', REPLY_KB)


async def _send_device_list(chat_id: int, view: dict) -> None:
    """Список устройств — инлайн-кнопки (drill-down к карточке устройства)."""
    devices = view.get('devices', [])
    if devices:
        await transport.tg_send(chat_id, _devices_text(view), 'Markdown', _devices_markup(view))
    else:
        await transport.tg_send(chat_id, _devices_text(view), 'Markdown')


async def _start_add_device(chat_id: int, view: dict) -> None:
    """Начать добавление устройства: проверить лимит, запросить название."""
    limit = view.get('deviceLimit')
    if limit is not None and len(view.get('devices', [])) >= limit:
        await transport.tg_send(chat_id, ERROR_TEXT['device_limit'])
        return
    _awaiting_device_name.add(chat_id)
    await transport.tg_send(chat_id, 'Пришлите название устройства (например: Телефон, Ноутбук).')


def _is_not_linked(e: 'BotApiError') -> bool:
    """Панель не знает этот чат: его не привязывали или отвязали посреди запроса."""
    return e.status == 404 and 'not_linked' in (e.code, e.message)


async def _client_error_reply(chat_id: int, e: 'BotApiError') -> None:
    """Единый ответ клиенту на ошибку /api/bot/*."""
    if e.status == 404:
        await transport.tg_send(chat_id, _no_access_text())
    else:
        await transport.tg_send(chat_id, f'❌ {e.message}')


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed_chat(update):
        return
    chat_id = update.effective_chat.id

    # Админский чат — текущее админское приветствие, без изменений.
    if chat_id == config.CHAT_ID:
        await transport.tg_reply(update, ADMIN_START_TEXT, 'Markdown')
        return

    payload = context.args[0] if context.args else None
    username = update.effective_user.username if update.effective_user else None

    if payload:
        # Диплинк-приглашение: t.me/bot?start=<пароль> → привязка чата.
        try:
            view = await panel.api_link(chat_id, payload, username)
        except BotApiError as e:
            if e.status == 404:
                await transport.tg_reply(
                    update,
                    'Ссылка недействительна. Запросите новое приглашение у администратора.',
                )
            else:
                await transport.tg_reply(update, f'❌ {e.message}')
            return
        except Exception as e:
            log.error('cmd_start link failed (%s)', type(e).__name__)
            await transport.tg_reply(update, f'❌ {UNEXPECTED_ERROR}')
            return
        await transport.tg_send(
            chat_id,
            f'✅ Доступ к VPN активирован для {md_bold(view.get("name", "?"))}.',
            'Markdown',
        )
        await _send_client_menu(chat_id, view)
        return

    # /start без payload — открыть меню, если чат уже привязан.
    try:
        view = await panel.api_client(chat_id)
    except BotApiError as e:
        if e.status == 404:
            await transport.tg_reply(update, _no_access_text())
        else:
            await transport.tg_reply(update, f'❌ {e.message}')
        return
    except Exception as e:
        log.error('cmd_start client failed (%s)', type(e).__name__)
        await transport.tg_reply(update, f'❌ {UNEXPECTED_ERROR}')
        return
    await _send_client_menu(chat_id, view)


async def _admin_only(update: Update) -> bool:
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or user.is_bot or chat.id != config.CHAT_ID:
        return False
    if chat.type == 'private':
        return user.id == chat.id
    if chat.type not in ('group', 'supergroup'):
        return False
    # Анонимные сообщения от имени группы не удостоверяют отправителя.
    if update.message and update.message.sender_chat:
        return False
    try:
        member = await transport._tg_post('getChatMember', {'chat_id': chat.id, 'user_id': user.id})
        return bool(member.get('ok') and member.get('result', {}).get('status')
                    in ('creator', 'administrator'))
    except Exception:
        log.warning('Не удалось проверить администратора Telegram-чата')
        return False


async def _allowed_chat(update: Update) -> bool:
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or user.is_bot:
        return False
    if chat.id == config.CHAT_ID:
        return await _admin_only(update)
    # Клиентская привязка chatId удостоверяет владельца только в личном чате.
    return chat.type == 'private' and user.id == chat.id


async def on_text(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Текст от клиентов: reply-кнопки навигации, ввод имени устройства, прочее.
    Админский чат — обрабатывается только при активном ожидании ввода
    (имя нового клиента / дата срока), иначе игнор (там команды)."""
    if not await _allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    text = (update.message.text or '').strip() if update.message else ''

    if chat_id == config.CHAT_ID:
        # При активном ожидании ввода _handle_admin_text обрабатывает текст
        # (имя нового клиента / дата срока); иначе обычный текст в админ-чате
        # игнорируется (там команды). В обоих случаях далее не идём.
        try:
            await admin._handle_admin_text(text)
        except BotApiError as e:
            await transport.tg_send(config.CHAT_ID, f'❌ {e.message}')
        except Exception as e:
            log.error('admin text failed (%s)', type(e).__name__)
            await transport.tg_send(config.CHAT_ID, f'❌ {UNEXPECTED_ERROR}')
        return

    # Reply-кнопки навигации имеют приоритет — даже если ждём имя устройства.
    if text in (NAV_DEVICES, NAV_ADD):
        _awaiting_device_name.discard(chat_id)
        try:
            view = await panel.api_client(chat_id)
        except BotApiError as e:
            await _client_error_reply(chat_id, e)
            return
        except Exception as e:
            log.error('on_text nav failed (%s)', type(e).__name__)
            await transport.tg_send(chat_id, f'❌ {UNEXPECTED_ERROR}')
            return
        if text == NAV_DEVICES:
            await _send_device_list(chat_id, view)
        else:
            await _start_add_device(chat_id, view)
        return

    # Чат ждёт название устройства — обрабатываем как имя.
    if chat_id in _awaiting_device_name:
        _awaiting_device_name.discard(chat_id)
        if not text:
            await transport.tg_send(chat_id, 'Название не распознано. Откройте меню: /start')
            return
        try:
            view = await panel.api_add_device(chat_id, text)
        except BotApiError as e:
            if _is_not_linked(e):
                await transport.tg_send(chat_id, _no_access_text())
            else:
                await transport.tg_send(chat_id, f'❌ {e.message}')
            return
        except Exception as e:
            log.error('add_device failed (%s)', type(e).__name__)
            await transport.tg_send(chat_id, f'❌ {UNEXPECTED_ERROR}')
            return
        await transport.tg_send(chat_id, f'✅ Устройство «{text}» добавлено.')
        await _send_client_menu(chat_id, view)
        return

    # Прочий текст от привязанного клиента — показать меню.
    try:
        view = await panel.api_client(chat_id)
    except BotApiError as e:
        await _client_error_reply(chat_id, e)
        return
    except Exception as e:
        log.error('on_text client failed (%s)', type(e).__name__)
        await transport.tg_send(chat_id, f'❌ {UNEXPECTED_ERROR}')
        return
    await _send_client_menu(chat_id, view)


# ── Client callback queries (инлайн-кнопки) ─────────────────────────────────────

async def on_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not await _allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    callback_id = query.id
    message_id = query.message.message_id if query.message else None
    data = query.data or ''

    # Админский чат — управление клиентами; остальные — клиентское меню.
    if chat_id == config.CHAT_ID:
        try:
            await admin._handle_admin_callback(message_id, callback_id, data)
        except BotApiError as e:
            await transport.tg_answer_callback(callback_id, e.message, show_alert=True)
        except Exception as e:
            log.error('admin callback failed (%s)', type(e).__name__)
            await transport.tg_answer_callback(callback_id, UNEXPECTED_ERROR, show_alert=True)
        return

    try:
        await _handle_callback(chat_id, message_id, callback_id, data)
    except BotApiError as e:
        if _is_not_linked(e):
            # Тот же ответ, что любому непривязанному чату. Сообщением: кнопку
            # выдачи конфига уже погасили «Готовлю конфиг…», второй ответ не дойдёт.
            await transport.tg_answer_callback(callback_id)
            await transport.tg_send(chat_id, _no_access_text())
        else:
            await _callback_alert(chat_id, callback_id, e.message)
    except Exception as e:
        log.error('callback failed (%s)', type(e).__name__)
        await _callback_alert(chat_id, callback_id, UNEXPECTED_ERROR)


async def _callback_alert(chat_id: int, callback_id: str, text: str) -> None:
    """Ошибка нажатия всплывашкой; кнопка уже погашена - сообщением в чат."""
    answer = await transport.tg_answer_callback(callback_id, text, show_alert=True)
    if isinstance(answer, dict) and not answer.get('ok'):
        await transport.tg_send(chat_id, f'❌ {text}')


async def _handle_callback(chat_id: int, message_id: int | None,
                           callback_id: str, data: str) -> None:
    action, _, arg = data.partition(':')

    if action == 'devs':
        view = await panel.api_client(chat_id)
        await transport.tg_answer_callback(callback_id)
        if message_id:
            await transport.tg_edit(chat_id, message_id, _devices_text(view), 'Markdown',
                          _devices_markup(view))
        return

    if action == 'dev':
        view = await panel.api_client(chat_id)
        device = _find_device(view, arg)
        if device is None:
            await transport.tg_answer_callback(callback_id, 'Устройство не найдено', show_alert=True)
            if message_id:
                await transport.tg_edit(chat_id, message_id, _devices_text(view), 'Markdown',
                              _devices_markup(view))
            return
        await transport.tg_answer_callback(callback_id)
        if message_id:
            await transport.tg_edit(chat_id, message_id, _device_card_text(device), 'Markdown',
                          _device_card_markup(arg))
        return

    if action == 'wg':
        view = await panel.api_client(chat_id)
        device = _find_device(view, arg)
        if device is None:
            await transport.tg_answer_callback(callback_id, 'Устройство не найдено', show_alert=True)
            return
        await transport.tg_answer_callback(callback_id, 'Готовлю конфиг WireGuard…')
        conf = await panel.api_wg_config(chat_id, arg)
        config_name = device.get('configName') or device.get('name') or 'wireguard'
        await transport.tg_send_qr(chat_id, conf, f'{md_bold(device.get("name", "?"))} — WireGuard')
        await transport.tg_send_document(
            chat_id, f'{config_name}.conf', conf.encode('utf-8'),
            caption=f'{md_bold(device.get("name", "?"))} — WireGuard',
        )
        return

    if action == 'ovpn':
        view = await panel.api_client(chat_id)
        device = _find_device(view, arg)
        if device is None:
            await transport.tg_answer_callback(callback_id, 'Устройство не найдено', show_alert=True)
            return
        await transport.tg_answer_callback(callback_id, 'Готовлю конфиг OpenVPN…')
        conf = await panel.api_ovpn_config(chat_id, arg)
        config_name = device.get('configName') or device.get('name') or 'openvpn'
        await transport.tg_send_document(
            chat_id, f'{config_name}.ovpn', conf.encode('utf-8'),
            caption=f'{md_bold(device.get("name", "?"))} — OpenVPN',
        )
        return

    if action == 'reic':
        # Запрос подтверждения перевыпуска.
        await transport.tg_answer_callback(callback_id)
        if message_id:
            await transport.tg_edit(
                chat_id, message_id,
                '🔄 *Перевыпустить ключи?*\n\n'
                'Старые конфиги перестанут работать — '
                'нужно будет выдать новые на все устройства.',
                'Markdown',
                _confirm_markup(f'reicyes:{arg}', f'dev:{arg}'),
            )
        return

    if action == 'reicyes':
        await transport.tg_answer_callback(callback_id, 'Перевыпускаю ключи…')
        view = await panel.api_reissue_device(chat_id, arg)
        device = _find_device(view, arg)
        if message_id and device is not None:
            await transport.tg_edit(
                chat_id, message_id,
                f'Перевыпуск выполняется\n\n{_device_card_text(device)}',
                'Markdown',
                _device_card_markup(arg),
            )
        elif message_id:
            await transport.tg_edit(chat_id, message_id, _devices_text(view), 'Markdown',
                          _devices_markup(view))
        return

    if action == 'del':
        # Запрос подтверждения удаления.
        view = await panel.api_client(chat_id)
        device = _find_device(view, arg)
        dev_name = device.get('name', '?') if device else '?'
        await transport.tg_answer_callback(callback_id)
        if message_id:
            await transport.tg_edit(
                chat_id, message_id,
                f'🗑 {md_bold(f"Удалить устройство «{dev_name}»?")}\n\n'
                'Конфиги этого устройства перестанут работать.',
                'Markdown',
                _confirm_markup(f'delyes:{arg}', f'dev:{arg}'),
            )
        return

    if action == 'delyes':
        await transport.tg_answer_callback(callback_id, 'Удаляю устройство…')
        view = await panel.api_delete_device(chat_id, arg)
        if message_id:
            await transport.tg_edit(chat_id, message_id, _devices_text(view), 'Markdown',
                          _devices_markup(view))
        return

    # Неизвестное действие — просто гасим «часики».
    await transport.tg_answer_callback(callback_id)
