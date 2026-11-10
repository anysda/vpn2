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
    out = []
    for part in _MD_SPECIAL.split(str(text)):
        if not part:
            continue
        out.append('\\' + part if _MD_SPECIAL.fullmatch(part) else f'*{part}*')
    return ''.join(out)


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
PLURAL: dict[str, tuple[str, str, str]] = {k: tuple(v) for k, v in _TABLE['plural'].items()}


def plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """Форма после целого числа: один, два, пять; как pluralRu в панели."""
    mod10 = abs(n) % 10
    mod100 = abs(n) % 100
    if mod10 == 1 and mod100 != 11:
        return forms[0]
    if 2 <= mod10 <= 4 and (mod100 < 10 or mod100 >= 20):
        return forms[1]
    return forms[2]


def _limit_label(device_limit) -> str:
    return '∞' if device_limit is None else str(device_limit)


def _expiry_label(expires_at) -> str:
    """Строка о сроке действия ключа — показывается клиенту в меню/приветствии."""
    if not expires_at:
        return 'Доступ бессрочный.'
    try:
        d = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
        return f'🗓 Ключ действует до {d.strftime("%d.%m.%Y")}.'
    except Exception:
        return ''


# Навигация клиента — постоянная reply-клавиатура прямо над полем ввода.
NAV_DEVICES = '📱 Мои устройства'
NAV_ADD = '➕ Добавить устройство'
REPLY_KB = {
    'keyboard': [[NAV_DEVICES, NAV_ADD]],
    'resize_keyboard': True,
    'is_persistent': True,
}


def _menu_text(view: dict) -> str:
    name = view.get('name', '?')
    count = len(view.get('devices', []))
    limit = _limit_label(view.get('deviceLimit'))
    return (
        f'🔐 *VPN-доступ* для {md_bold(name)}.\n'
        f'Устройства: {count} из {limit}.\n'
        f'{_expiry_label(view.get("expiresAt"))}'
    )


def _devices_markup(view: dict) -> dict:
    rows = []
    for d in view.get('devices', []):
        rows.append([{
            'text': f'📱 {d.get("name", "?")}',
            'callback_data': f'dev:{d.get("id")}',
        }])
    return {'inline_keyboard': rows}


def _access_protocol_lines(view: dict) -> str:
    operation = view.get('operation') or {}
    states = operation.get('protocols') or {}
    lines = []
    for protocol, label in (('wg', 'WireGuard'), ('openvpn', 'OpenVPN'), ('ikev2', 'IKEv2')):
        state = states.get(protocol)
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


def _devices_text(view: dict) -> str:
    devices = view.get('devices', [])
    if not devices:
        return 'У вас пока нет устройств. Нажмите «➕ Добавить устройство» в меню.'
    limit = _limit_label(view.get('deviceLimit'))
    return (f'📱 *Ваши устройства* ({len(devices)}/{limit})\n'
            f'{md(view.get("accessStatus", "Состояние не подтверждено"))}\n'
            f'{_access_protocol_lines(view)}Выберите устройство:')


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
    return f'📱 {md_bold(name)}\nПротоколы: {proto}\n\nВыберите действие:'


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

UNEXPECTED_ERROR = 'Сервис временно недоступен. Попробуйте позже.'

ADMIN_START_TEXT = (
    '*anysda-vpn*\n\n'
    '/status — состояние системы\n'
    '/nodes  — RTT по exit-нодам\n'
    '/clients — клиенты'
)


def _no_access_text() -> str:
    """Сообщение непривязанному человеку: как получить доступ к VPN.

    Если известен никнейм админа — предлагаем написать ему напрямую,
    иначе фолбэк-инструкция про ссылку-приглашение."""
    if config.ADMIN_USERNAME:
        return (
            '🔑 Чтобы получить доступ к VPN, напишите администратору: '
            f'@{config.ADMIN_USERNAME}'
        )
    return 'Чтобы пользоваться VPN, откройте ссылку-приглашение от администратора.'
