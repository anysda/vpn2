"""Всё, что бот говорит людям: тексты, клавиатуры и разметка."""
import json
import re
from datetime import datetime
from pathlib import Path

from . import config

# Разметка — legacy Markdown. Имена клиентов и устройств приходят от людей,
# а «_», «*», «`», «[» в них ломают разбор: Telegram отвечает ok:false и
# сообщение не уходит вовсе (клиент ivan_petrov оставался без меню).
_MD_SPECIAL = re.compile(r'([_*`\[])')


def md(text: object) -> str:
    """Экранировать текст для Markdown вне сущностей."""
    return _MD_SPECIAL.sub(r'\\\1', str(text))


def md_bold(text: object) -> str:
    """Жирный текст с любыми символами. Внутри сущности экранировать нельзя,
    поэтому спецсимвол выносится наружу: «a_b» → «*a*\\_*b*»."""
    return ''.join('\\' + part if _MD_SPECIAL.fullmatch(part) else f'*{part}*'
                   for part in _MD_SPECIAL.split(str(text)) if part)


def _load_table() -> dict:
    # Shared with the panel: error texts and plural forms live in one file.
    # The image ships it inside the package, the repo keeps it in app/shared.
    try:
        raw = Path(__file__).with_name('error-text.json').read_text(encoding='utf-8')
    except FileNotFoundError:
        shared = Path(__file__).resolve().parents[2] / 'app' / 'shared' / 'error-text.json'
        raw = shared.read_text(encoding='utf-8')
    return json.loads(raw)


_TABLE = _load_table()
# Коды /api/bot/* (statusMessage панели) → текст для человека в Telegram.
# Код машинный: показывать его клиенту нельзя, он уходит в журнал.
ERROR_TEXT: dict[str, str] = _TABLE['bot']
ERROR_FALLBACK: str = _TABLE['botFallback']
UNEXPECTED_ERROR = 'Сервис временно недоступен. Попробуйте позже.'


def plural_ru(n: int, forms: list[str]) -> str:
    """Форма после целого числа: один, два, пять; как pluralRu в панели."""
    mod10, mod100 = abs(n) % 10, abs(n) % 100
    if mod10 == 1 and mod100 != 11:
        return forms[0]
    if 2 <= mod10 <= 4 and (mod100 < 10 or mod100 >= 20):
        return forms[1]
    return forms[2]


def _limit(device_limit) -> str:
    return '∞' if device_limit is None else str(device_limit)


def _date(iso) -> str:
    return datetime.fromisoformat(str(iso).replace('Z', '+00:00')).strftime('%d.%m.%Y')


def _protocol_lines(view: dict) -> str:
    """Что подтверждено по каждому протоколу после последней операции с доступом."""
    operation = view.get('operation') or {}
    lines = []
    for protocol, label in (('wg', 'WireGuard'), ('openvpn', 'OpenVPN'), ('ikev2', 'IKEv2')):
        state = (operation.get('protocols') or {}).get(protocol)
        if not state:
            continue
        if state.get('state') == 'not_required':
            text = 'Не требуется'
        elif operation.get('status') == 'superseded':
            text = 'Заменена'
        elif state.get('state') == 'confirmed' and view.get('accessStatus') != 'Состояние не подтверждено':
            text = 'Подтверждено'
        else:
            text = state.get('error') or 'Состояние не подтверждено'
        lines.append(f'{label}: {text}')
    return md('\n'.join(lines) + '\n') if lines else ''


def confirm_markup(yes_data: str, no_data: str) -> dict:
    return {'inline_keyboard': [[{'text': '✅ Да', 'callback_data': yes_data},
                                 {'text': '✖ Отмена', 'callback_data': no_data}]]}


# ── Клиент ─────────────────────────────────────────────────────────────────────

# Навигация клиента — постоянная reply-клавиатура прямо над полем ввода.
NAV_DEVICES = '📱 Мои устройства'
NAV_ADD = '➕ Добавить устройство'
REPLY_KB = {'keyboard': [[NAV_DEVICES, NAV_ADD]], 'resize_keyboard': True, 'is_persistent': True}

BAD_INVITE = 'Ссылка недействительна. Запросите новое приглашение у администратора.'
ASK_DEVICE_NAME = 'Пришлите название устройства (например: Телефон, Ноутбук).'
NO_DEVICE_NAME = 'Название не распознано. Откройте меню: /start'
NO_DEVICE = 'Устройство не найдено'
REISSUE_CONFIRM = ('🔄 *Перевыпустить ключи?*\n\n'
                   'Старые конфиги перестанут работать — '
                   'нужно будет выдать новые на все устройства.')
# Кнопка выдачи конфига → название протокола, имя файла по умолчанию, расширение.
CONFIGS = {'wg': ('WireGuard', 'wireguard', 'conf'), 'ovpn': ('OpenVPN', 'openvpn', 'ovpn')}


def no_access() -> str:
    """Непривязанному человеку: как получить доступ к VPN."""
    if config.ADMIN_USERNAME:
        return f'🔑 Чтобы получить доступ к VPN, напишите администратору: @{config.ADMIN_USERNAME}'
    return 'Чтобы пользоваться VPN, откройте ссылку-приглашение от администратора.'


def menu(view: dict) -> str:
    expires = view.get('expiresAt')
    try:
        expiry = f'🗓 Ключ действует до {_date(expires)}.' if expires else 'Доступ бессрочный.'
    except Exception:
        expiry = ''
    return (f'🔐 *VPN-доступ* для {md_bold(view.get("name", "?"))}.\n'
            f'Устройства: {len(view.get("devices", []))} из {_limit(view.get("deviceLimit"))}.\n'
            f'{expiry}')


def devices(view: dict) -> str:
    count = len(view.get('devices', []))
    if not count:
        return 'У вас пока нет устройств. Нажмите «➕ Добавить устройство» в меню.'
    return (f'📱 *Ваши устройства* ({count}/{_limit(view.get("deviceLimit"))})\n'
            f'{md(view.get("accessStatus", "Состояние не подтверждено"))}\n'
            f'{_protocol_lines(view)}Выберите устройство:')


def devices_markup(view: dict) -> dict:
    return {'inline_keyboard': [
        [{'text': f'📱 {d.get("name", "?")}', 'callback_data': f'dev:{d.get("id")}'}]
        for d in view.get('devices', [])]}


def find_device(view: dict, device_id: str) -> dict | None:
    return next((d for d in view.get('devices', []) if str(d.get('id')) == device_id), None)


def device_card(device: dict) -> str:
    protocols = [label for flag, label in (('hasWg', 'WireGuard'), ('hasOvpn', 'OpenVPN'))
                 if device.get(flag)]
    return (f'📱 {md_bold(device.get("name", "?"))}\n'
            f'Протоколы: {", ".join(protocols) or "нет конфигов"}\n\nВыберите действие:')


def device_card_markup(device_id: str) -> dict:
    return {'inline_keyboard': [
        [{'text': 'WireGuard', 'callback_data': f'wg:{device_id}'},
         {'text': 'OpenVPN', 'callback_data': f'ovpn:{device_id}'}],
        [{'text': '🔄 Перевыпустить ключи', 'callback_data': f'reic:{device_id}'},
         {'text': '🗑 Удалить', 'callback_data': f'del:{device_id}'}],
        [{'text': '‹ Назад', 'callback_data': 'devs'}],
    ]}


# ── Администратор ──────────────────────────────────────────────────────────────

ADMIN_START = ('*anysda-vpn*\n\n'
               '/status — состояние системы\n'
               '/nodes  — RTT по exit-нодам\n'
               '/clients — клиенты')
ASK_CLIENT_NAME = '➕ Пришлите имя нового клиента.'
NO_CLIENT_NAME = 'Имя не распознано. Пришлите имя клиента ещё раз.'
ASK_EXPIRY = '🗓 Пришлите дату окончания доступа в формате *ДД.ММ.ГГГГ* или слово «бессрочно».'
BAD_EXPIRY = '⚠️ Неверный формат. Пришлите дату как *ДД.ММ.ГГГГ* или слово «бессрочно».'
NEVER_EXPIRES = ('бессрочно', 'безсрочно', 'infinite', 'never')
# Состояния доступа, при которых операция завершена; при прочих у клиента «⏳».
_SETTLED = ('Доступ включён', 'Доступ отключён', 'Ключи перевыпущены')
_PLURAL_DEVICE = _TABLE['plural']['botDevice']


CLIENT_LIST = '👥 *Клиенты* ({} шт.)\nВыберите клиента для управления:'


def clients_markup(items: list[dict]) -> dict:
    rows = []
    for c in items:
        verified = c.get('accessStatus')
        icon = '✅' if c.get('status', '') == 'active' else '❄️'
        if verified and verified not in _SETTLED:
            icon = '⏳'
        count = c.get('deviceCount', 0)
        rows.append([{
            'text': f'{icon} {c.get("name", "?")} — {count} {plural_ru(count, _PLURAL_DEVICE)}'
                    f' · лимит: {_limit(c.get("deviceLimit"))}'
                    + (f' · {verified}' if verified else ''),
            'callback_data': f'acl:{c.get("id")}',
        }])
    rows.append([{'text': '➕ Создать клиента', 'callback_data': 'anew'}])
    return {'inline_keyboard': rows}


def client_card(c: dict) -> str:
    expires = c.get('expiresAt')
    try:
        expiry = _date(expires) if expires else 'бессрочно'
    except Exception:
        expiry = str(expires)
    return (f'👤 {md_bold(c.get("name", "?"))}\n'
            f'Желаемое состояние: {"активен ✅" if c.get("status", "") == "active" else "заморожен ❄️"}\n'
            f'{md(c.get("accessStatus", "Состояние не подтверждено"))}\n'
            f'{_protocol_lines(c)}'
            f'Устройств: {c.get("deviceCount", 0)}/{_limit(c.get("deviceLimit"))}\n'
            f'Срок действия: {md(expiry)}\n'
            f'Telegram: {"привязан ✅" if c.get("tgLinked") else "не привязан ✖"}')


def client_card_markup(c: dict) -> dict:
    cid = c.get('id')
    operation = c.get('operation') or {}
    frozen = bool(c.get('frozenManual')) or c.get('status') == 'frozen'
    # «Повторить» — пока операция с доступом не подтверждена и не заменена новой.
    can_retry = (operation and operation.get('status') != 'superseded'
                 and (operation.get('status') == 'pending'
                      or c.get('accessStatus') == 'Состояние не подтверждено'))
    return {'inline_keyboard': [
        [{'text': '☀️ Разморозить' if frozen else '❄️ Заморозить', 'callback_data': f'afz:{cid}'}],
        *([[{'text': 'Повторить', 'callback_data': f'aretry:{cid}'}]] if can_retry else []),
        [{'text': '➖ Лимит', 'callback_data': f'alm-:{cid}'},
         {'text': '➕ Лимит', 'callback_data': f'alm+:{cid}'}],
        [{'text': '🗓 Срок', 'callback_data': f'aexp:{cid}'}],
        [{'text': '📨 Отправить приглашение', 'callback_data': f'ainv:{cid}'}],
        [{'text': '🗑 Удалить', 'callback_data': f'adel:{cid}'}],
        [{'text': '‹ К списку', 'callback_data': 'acl'}],
    ]}


# ── Состояние системы, тревоги, приглашения ────────────────────────────────────

# Контракт app/shared/outbounds.ts: hy2-us-direct → US, hy2-de-warp → DE WARP.
_EXIT_OUTBOUND = re.compile(r'hy2-([a-z](?:[a-z0-9-]*[a-z0-9])?)-(direct|warp)')


def _pct(value) -> str:
    return f'{value:.0f}%' if isinstance(value, (int, float)) else '—'


def _exit_lines(data: dict) -> list[str]:
    lines = []
    for outbound in data.get('outbounds', []):
        match = _EXIT_OUTBOUND.fullmatch(outbound.get('name') or '')
        if not match:
            continue
        label = match[1].upper() + (' WARP' if match[2] == 'warp' else '')
        rtt = outbound.get('rttMs')
        lines.append(f'{"🟢" if rtt else "🔴"} `{label}`: {f"{rtt} ms" if rtt else "нет связи"}')
    return lines


def status(data: dict, header: str = '*Состояние системы*') -> str:
    lines = [header + '\n']
    for n in data.get('nodes', []):
        net = (n.get('rxMbps') or 0) + (n.get('txMbps') or 0)
        lines.append(f'🖥 *{n.get("tag", "?").upper()}*: CPU {_pct(n.get("cpu"))}'
                     f' | RAM {_pct(n.get("ram"))} | ↕ {net:.1f} Mbps')
    return '\n'.join([*lines, '', *_exit_lines(data)])


def nodes(data: dict) -> str:
    return '\n'.join(['*Exit-ноды*\n', *_exit_lines(data)]) if _exit_lines(data) else 'Нет нод'


DEPLOY_DONE = '✅ *Установка завершена*'
DEPLOY_WARNED = '⚠️ *Установка завершена с предупреждениями*'
DEPLOY_UNMEASURED = ('\n\n? Не проверено: часть проверок не измерена. '
                     'Причины — в сводке деплоя 99-verify.')
PANEL_DOWN = '🔴 Панель недоступна — мониторинговые данные не поступают'
PANEL_UP = '🟢 Панель снова доступна'


# Тревоги монитора: нода, затем минуты | метрика, порог, значение.
NODE_DOWN = '🔴 *Нода {} лежит* — метрик нет больше {} мин'
NODE_UP = '🟢 *Нода {} восстановилась*'
LOAD_HIGH = '⚠️ *{}*: нагрузка на {} выше {:.0f}% ({:.0f}%)'
LOAD_OK = '✅ *{}*: {} вернулся в норму ({:.0f}%)'


def invite(password: str) -> str:
    """Готовое приветствие, которое админ пересылает клиенту."""
    if config.BOT_USERNAME:
        return ('🔐 Вам предоставлен доступ к VPN. Откройте ссылку и нажмите «Запустить»:\n'
                f'https://t.me/{config.BOT_USERNAME}?start={password}')
    # Имя бота неизвестно (getMe не ответил) — текстовая инструкция.
    return ('🔐 Вам предоставлен доступ к VPN.\n'
            'Найдите нашего бота в Telegram и отправьте ему сообщение:\n'
            f'/start {password}')


# ── Короткие ответы; «{}» — имя или число ──────────────────────────────────────

LINKED = '✅ Доступ к VPN активирован для {}.'
DEVICE_ADDED = '✅ Устройство «{}» добавлено.'
PREPARING = 'Готовлю конфиг {}…'
REISSUING = 'Перевыпускаю ключи…'
REISSUE_STARTED = 'Перевыпуск выполняется\n\n{}'
DELETING = 'Удаляю устройство…'
DELETE_DEVICE = '🗑 {}\n\nКонфиги этого устройства перестанут работать.'
DELETE_DEVICE_ASK = 'Удалить устройство «{}»?'
DELETE_CLIENT = '🗑 {}\n\nВсе его устройства и конфиги перестанут работать.'
DELETE_CLIENT_ASK = 'Удалить клиента «{}»?'
CLIENT_CREATED = '✅ Клиент «{}» создан.'
EXPIRY_CLEARED = 'Срок действия снят (бессрочно).'
EXPIRY_SET = 'Срок действия обновлён.'
ACCESS_OFF = 'Отключение запущено'
ACCESS_ON = 'Включение запущено'
NO_OPERATION = 'Операция не найдена'
RETRY_STARTED = 'Повтор запущен'
LIMIT_UNLIMITED = 'Лимит уже безлимитный'
LIMIT_SET = 'Лимит: {}'
NO_PASSWORD = 'У клиента нет пароля для приглашения'
SENDING_INVITE = 'Отправляю приглашение…'
INVITE_NOTE = '📨 Приглашение для «{}». Перешлите клиенту сообщение ниже ↓'
NEW_CLIENT = '👤 Новый клиент: {}'
IKEV2 = 'Сервер: `{}`\nЛогин: `{}`\nПароль: `{}`\n\n{} — IKEv2'
