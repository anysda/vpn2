"""Texts, cards and keyboards shared by several handlers, and the legacy Markdown escaping."""
import json
import re
from datetime import datetime
from pathlib import Path

from . import config

# Names come from people; an unescaped _ * ` [ makes Telegram refuse the whole message.
_MD_SPECIAL = re.compile(r'([_*`\[])')


def md(text: object) -> str:
    return _MD_SPECIAL.sub(r'\\\1', str(text))


def md_bold(text: object) -> str:
    """Bold with any characters: escaping is impossible inside an entity, so «a_b» → «*a*\\_*b*»."""
    return ''.join('\\' + part if _MD_SPECIAL.fullmatch(part) else f'*{part}*'
                   for part in _MD_SPECIAL.split(str(text)) if part)


# Error texts and plural forms shared with the panel: inside the package in the image, app/shared in the repo.
_TABLE_FILE = Path(__file__).with_name('error-text.json')
if not _TABLE_FILE.exists():
    _TABLE_FILE = Path(__file__).resolve().parents[2] / 'app' / 'shared' / 'error-text.json'
_TABLE = json.loads(_TABLE_FILE.read_text(encoding='utf-8'))
# Panel codes of /api/bot/* → text for the person; the code itself goes only to the log.
ERROR_TEXT: dict[str, str] = _TABLE['bot']
ERROR_FALLBACK: str = _TABLE['botFallback']
NO_DEVICE = 'Устройство не найдено'
ACCESS_OFF = 'Отключение запущено'
NAV_DEVICES = '📱 Мои устройства'
NAV_ADD = '➕ Добавить устройство'
REPLY_KB = {'keyboard': [[NAV_DEVICES, NAV_ADD]], 'resize_keyboard': True, 'is_persistent': True}


def plural_ru(n: int, forms: list[str]) -> str:
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


def keyboard(*rows: list[tuple[str, str]]) -> dict:
    """Inline keyboard from rows of (button text, callback data)."""
    return {'inline_keyboard': [[{'text': text, 'callback_data': data} for text, data in row] for row in rows]}


def confirm_markup(yes_data: str, no_data: str) -> dict:
    return keyboard([('✅ Да', yes_data), ('✖ Отмена', no_data)])


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
    return keyboard(*([(f'📱 {d.get("name", "?")}', f'dev:{d.get("id")}')] for d in view.get('devices', [])))


def find_device(view: dict, device_id: str) -> dict | None:
    return next((d for d in view.get('devices', []) if str(d.get('id')) == device_id), None)


def device_card(device: dict) -> str:
    protocols = [label for flag, label in (('hasWg', 'WireGuard'), ('hasOvpn', 'OpenVPN')) if device.get(flag)]
    return (f'📱 {md_bold(device.get("name", "?"))}\n'
            f'Протоколы: {", ".join(protocols) or "нет конфигов"}\n\nВыберите действие:')


def device_card_markup(device_id: str) -> dict:
    return keyboard([('WireGuard', f'wg:{device_id}'), ('OpenVPN', f'ovpn:{device_id}')],
                    [('🔄 Перевыпустить ключи', f'reic:{device_id}'), ('🗑 Удалить', f'del:{device_id}')],
                    [('‹ Назад', 'devs')])


# Access states of a finished operation; any other one shows «⏳» in the client list.
_SETTLED = ('Доступ включён', 'Доступ отключён', 'Ключи перевыпущены')


def clients_markup(items: list[dict]) -> dict:
    rows = []
    for c in items:
        verified = c.get('accessStatus')
        icon = '✅' if c.get('status', '') == 'active' else '❄️'
        if verified and verified not in _SETTLED:
            icon = '⏳'
        count = c.get('deviceCount', 0)
        text = (f'{icon} {c.get("name", "?")} — {count} {plural_ru(count, _TABLE["plural"]["botDevice"])}'
                f' · лимит: {_limit(c.get("deviceLimit"))}' + (f' · {verified}' if verified else ''))
        rows.append([(text, f'acl:{c.get("id")}')])
    return keyboard(*rows, [('➕ Создать клиента', 'anew')])


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
    can_retry = (operation and operation.get('status') != 'superseded'
                 and (operation.get('status') == 'pending'
                      or c.get('accessStatus') == 'Состояние не подтверждено'))
    return keyboard([('☀️ Разморозить' if frozen else '❄️ Заморозить', f'afz:{cid}')],
                    *([[('Повторить', f'aretry:{cid}')]] if can_retry else []),
                    [('➖ Лимит', f'alm-:{cid}'), ('➕ Лимит', f'alm+:{cid}')],
                    [('🗓 Срок', f'aexp:{cid}')],
                    [('📨 Отправить приглашение', f'ainv:{cid}')],
                    [('🗑 Удалить', f'adel:{cid}')],
                    [('‹ К списку', 'acl')])


# Outbound names of app/shared/outbounds.ts: hy2-us-direct → US, hy2-de-warp → DE WARP.
_EXIT_OUTBOUND = re.compile(r'hy2-([a-z](?:[a-z0-9-]*[a-z0-9])?)-(direct|warp)')


def _pct(value) -> str:
    return f'{value:.0f}%' if isinstance(value, (int, float)) else '—'


def status(data: dict, header: str = '*Состояние системы*') -> str:
    lines = [header + '\n']
    for n in data.get('nodes', []):
        net = (n.get('rxMbps') or 0) + (n.get('txMbps') or 0)
        lines.append(f'🖥 *{n.get("tag", "?").upper()}*: CPU {_pct(n.get("cpu"))}'
                     f' | RAM {_pct(n.get("ram"))} | ↕ {net:.1f} Mbps')
    lines.append('')
    for outbound in data.get('outbounds', []):
        match = _EXIT_OUTBOUND.fullmatch(outbound.get('name') or '')
        if match:
            label = match[1].upper() + (' WARP' if match[2] == 'warp' else '')
            rtt = outbound.get('rttMs')
            lines.append(f'{"🟢" if rtt else "🔴"} `{label}`: {f"{rtt} ms" if rtt else "нет связи"}')
    return '\n'.join(lines)


def invite(password: str) -> str:
    """The greeting the admin forwards to the client; a text instruction when getMe gave no bot name."""
    if config.BOT_USERNAME:
        return ('🔐 Вам предоставлен доступ к VPN. Откройте ссылку и нажмите «Запустить»:\n'
                f'https://t.me/{config.BOT_USERNAME}?start={password}')
    return ('🔐 Вам предоставлен доступ к VPN.\nНайдите нашего бота в Telegram и отправьте ему сообщение:\n'
            f'/start {password}')
