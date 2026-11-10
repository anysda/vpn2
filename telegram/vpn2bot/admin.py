import re
from datetime import datetime

from . import config, events, panel, transport
from .panel import BotApiError
from .texts import (
    _access_protocol_lines,
    _confirm_markup,
    _limit_label,
    md,
    md_bold,
    plural_ru,
)


# Состояние ожидания ввода в АДМИНСКОМ чате. Значение:
#   ('new_client', None)   — ждём имя нового клиента;
#   ('expiry', '<id>')     — ждём дату срока действия для клиента <id>.
# Только один контекст ожидания одновременно (админский чат один).
_admin_await: dict | None = None


def _client_status_icon(status: str) -> str:
    return '✅' if status == 'active' else '❄️'


def _admin_list_text(clients: list[dict]) -> str:
    return f'👥 *Клиенты* ({len(clients)} шт.)\nВыберите клиента для управления:'


def _admin_list_markup(clients: list[dict]) -> dict:
    rows = []
    for c in clients:
        icon = _client_status_icon(c.get('status', ''))
        verified = c.get('accessStatus')
        if verified and verified not in ('Доступ включён', 'Доступ отключён', 'Ключи перевыпущены'):
            icon = '⏳'
        limit = _limit_label(c.get('deviceLimit'))
        count = c.get('deviceCount', 0)
        noun = plural_ru(count, ('девайс', 'девайса', 'девайсов'))
        rows.append([{
            'text': f'{icon} {c.get("name", "?")} — {count} {noun} · лимит: {limit}'
                    + (f' · {verified}' if verified else ''),
            'callback_data': f'acl:{c.get("id")}',
        }])
    rows.append([{'text': '➕ Создать клиента', 'callback_data': 'anew'}])
    return {'inline_keyboard': rows}


def _admin_card_text(c: dict) -> str:
    status = c.get('status', '')
    status_txt = 'активен ✅' if status == 'active' else 'заморожен ❄️'
    limit = _limit_label(c.get('deviceLimit'))
    count = c.get('deviceCount', 0)
    expires = c.get('expiresAt')
    if expires:
        try:
            d = datetime.fromisoformat(str(expires).replace('Z', '+00:00'))
            exp_txt = d.strftime('%d.%m.%Y')
        except Exception:
            exp_txt = str(expires)
    else:
        exp_txt = 'бессрочно'
    tg_txt = 'привязан ✅' if c.get('tgLinked') else 'не привязан ✖'
    return (
        f'👤 {md_bold(c.get("name", "?"))}\n'
        f'Желаемое состояние: {status_txt}\n'
        f'{md(c.get("accessStatus", "Состояние не подтверждено"))}\n'
        f'{_access_protocol_lines(c)}'
        f'Устройств: {count}/{limit}\n'
        f'Срок действия: {md(exp_txt)}\n'
        f'Telegram: {tg_txt}'
    )


def _admin_card_markup(c: dict) -> dict:
    cid = c.get('id')
    operation = c.get('operation') or {}
    frozen = bool(c.get('frozenManual')) or c.get('status') == 'frozen'
    freeze_btn = ({'text': '☀️ Разморозить', 'callback_data': f'afz:{cid}'}
                  if frozen else
                  {'text': '❄️ Заморозить', 'callback_data': f'afz:{cid}'})
    return {'inline_keyboard': [
        [freeze_btn],
        *([[{'text': 'Повторить', 'callback_data': f'aretry:{cid}'}]]
          if operation and operation.get('status') != 'superseded'
          and (operation.get('status') == 'pending' or c.get('accessStatus') == 'Состояние не подтверждено') else []),
        [
            {'text': '➖ Лимит', 'callback_data': f'alm-:{cid}'},
            {'text': '➕ Лимит', 'callback_data': f'alm+:{cid}'},
        ],
        [{'text': '🗓 Срок', 'callback_data': f'aexp:{cid}'}],
        [{'text': '📨 Отправить приглашение', 'callback_data': f'ainv:{cid}'}],
        [{'text': '🗑 Удалить', 'callback_data': f'adel:{cid}'}],
        [{'text': '‹ К списку', 'callback_data': 'acl'}],
    ]}


async def _admin_show_list(message_id: int | None) -> None:
    """Перерисовать сообщение в список клиентов (или прислать новое)."""
    clients = await panel.api_admin_clients()
    text = _admin_list_text(clients)
    markup = _admin_list_markup(clients)
    if message_id:
        await transport.tg_edit(config.CHAT_ID, message_id, text, 'Markdown', markup)
    else:
        await transport.tg_send(config.CHAT_ID, text, 'Markdown', markup)


async def _admin_show_card(message_id: int | None, client_id: str) -> None:
    """Перерисовать сообщение в карточку клиента (или прислать новое)."""
    c = await panel.api_admin_client(client_id)
    text = _admin_card_text(c)
    markup = _admin_card_markup(c)
    if message_id:
        await transport.tg_edit(config.CHAT_ID, message_id, text, 'Markdown', markup)
    else:
        await transport.tg_send(config.CHAT_ID, text, 'Markdown', markup)


async def cmd_clients_interactive(message_id: int | None = None) -> None:
    """Интерактивный /clients — список с инлайн-кнопками по каждому клиенту."""
    await _admin_show_list(message_id)


async def _handle_admin_callback(message_id: int | None, callback_id: str,
                                  data: str) -> None:
    """Роутер инлайн-кнопок админского управления клиентами."""
    global _admin_await
    action, _, arg = data.partition(':')

    if action == 'acl' and not arg:
        await transport.tg_answer_callback(callback_id)
        await _admin_show_list(message_id)
        return

    if action == 'acl':
        await transport.tg_answer_callback(callback_id)
        await _admin_show_card(message_id, arg)
        return

    if action == 'anew':
        _admin_await = {'kind': 'new_client', 'client_id': None}
        await transport.tg_answer_callback(callback_id)
        await transport.tg_send(config.CHAT_ID, '➕ Пришлите имя нового клиента.')
        return

    if action == 'afz':
        # Toggle frozenManual: читаем текущее состояние и инвертируем.
        c = await panel.api_admin_client(arg)
        new_frozen = not bool(c.get('frozenManual'))
        try:
            await panel.api_admin_patch_client(arg, {'frozenManual': new_frozen})
        except BotApiError as e:
            await transport.tg_answer_callback(callback_id, e.message, show_alert=True)
            return
        await transport.tg_answer_callback(
            callback_id, 'Отключение запущено' if new_frozen else 'Включение запущено')
        await _admin_show_card(message_id, arg)
        return

    if action == 'aretry':
        c = await panel.api_admin_client(arg)
        operation = c.get('operation') or {}
        operation_id = operation.get('id', '')
        if not re.fullmatch(r'[a-f0-9-]{36}', operation_id):
            await transport.tg_answer_callback(callback_id, 'Операция не найдена', show_alert=True)
            return
        await panel._bot_request('POST', f'/api/bot/admin/access-operations/{operation_id}/retry')
        await transport.tg_answer_callback(callback_id, 'Повтор запущен')
        await _admin_show_card(message_id, arg)
        return

    if action in ('alm+', 'alm-'):
        c = await panel.api_admin_client(arg)
        cur_limit = c.get('deviceLimit')
        dev_count = c.get('deviceCount', 0)
        if action == 'alm+':
            # ➕: безлимит остаётся безлимитом; иначе +1.
            if cur_limit is None:
                await transport.tg_answer_callback(callback_id, 'Лимит уже безлимитный')
                return
            new_limit = cur_limit + 1
        else:
            # ➖: при безлимите ставим число = кол-во устройств (мин. 1);
            # иначе -1. Ниже текущего кол-ва не опускаем сами — но бэк всё
            # равно проверит и вернёт 409.
            if cur_limit is None:
                new_limit = max(dev_count, 1)
            else:
                new_limit = cur_limit - 1
        try:
            await panel.api_admin_patch_client(arg, {'deviceLimit': new_limit})
        except BotApiError as e:
            await transport.tg_answer_callback(callback_id, e.message, show_alert=True)
            return
        await transport.tg_answer_callback(callback_id, f'Лимит: {new_limit}')
        await _admin_show_card(message_id, arg)
        return

    if action == 'aexp':
        _admin_await = {'kind': 'expiry', 'client_id': arg}
        await transport.tg_answer_callback(callback_id)
        await transport.tg_send(
            config.CHAT_ID,
            '🗓 Пришлите дату окончания доступа в формате *ДД.ММ.ГГГГ* '
            'или слово «бессрочно».',
            'Markdown',
        )
        return

    if action == 'ainv':
        c = await panel.api_admin_client(arg)
        password = c.get('password', '')
        if not password:
            await transport.tg_answer_callback(
                callback_id, 'У клиента нет пароля для приглашения', show_alert=True)
            return
        await transport.tg_answer_callback(callback_id, 'Отправляю приглашение…')
        await events._send_invite(c.get('name', '?'), password)
        return

    if action == 'adel':
        c = await panel.api_admin_client(arg)
        await transport.tg_answer_callback(callback_id)
        if message_id:
            await transport.tg_edit(
                config.CHAT_ID, message_id,
                '🗑 ' + md_bold(f'Удалить клиента «{c.get("name", "?")}»?') + '\n\n'
                'Все его устройства и конфиги перестанут работать.',
                'Markdown',
                _confirm_markup(f'adyes:{arg}', f'acl:{arg}'),
            )
        return

    if action == 'adyes':
        try:
            await panel.api_admin_delete_client(arg)
        except BotApiError as e:
            await transport.tg_answer_callback(callback_id, e.message, show_alert=True)
            return
        await transport.tg_answer_callback(callback_id, 'Отключение запущено')
        await _admin_show_list(message_id)
        return

    # Неизвестное действие — гасим «часики».
    await transport.tg_answer_callback(callback_id)


async def _handle_admin_text(text: str) -> bool:
    """Текст из админского чата при активном ожидании ввода.

    Возвращает True, если сообщение обработано (был активный ввод),
    иначе False — вызывающий обрабатывает как обычно (return)."""
    global _admin_await
    if _admin_await is None:
        return False

    kind = _admin_await.get('kind')

    if kind == 'new_client':
        name = text.strip()
        if not name:
            await transport.tg_send(config.CHAT_ID, 'Имя не распознано. Пришлите имя клиента ещё раз.')
            return True
        _admin_await = None
        try:
            c = await panel.api_admin_create_client(name)
        except BotApiError as e:
            await transport.tg_send(config.CHAT_ID, f'❌ {e.message}')
            return True
        await transport.tg_send(config.CHAT_ID, f'✅ Клиент «{name}» создан.')
        await _admin_show_card(None, str(c.get('id')))
        return True

    if kind == 'expiry':
        client_id = _admin_await.get('client_id')
        raw = text.strip()
        low = raw.lower()
        if low in ('бессрочно', 'безсрочно', 'infinite', 'never'):
            expires_iso = None
        else:
            try:
                d = datetime.strptime(raw, '%d.%m.%Y')
                expires_iso = d.strftime('%Y-%m-%dT00:00:00.000Z')
            except ValueError:
                await transport.tg_send(
                    config.CHAT_ID,
                    '⚠️ Неверный формат. Пришлите дату как *ДД.ММ.ГГГГ* '
                    'или слово «бессрочно».',
                    'Markdown',
                )
                return True
        _admin_await = None
        try:
            await panel.api_admin_patch_client(str(client_id), {'expiresAt': expires_iso})
        except BotApiError as e:
            await transport.tg_send(config.CHAT_ID, f'❌ {e.message}')
            return True
        await transport.tg_send(
            config.CHAT_ID,
            'Срок действия снят (бессрочно).' if expires_iso is None
            else 'Срок действия обновлён.',
        )
        await _admin_show_card(None, str(client_id))
        return True

    # Неизвестный вид ожидания — сбрасываем.
    _admin_await = None
    return False
