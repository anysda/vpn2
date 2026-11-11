"""Тексты аварийных сообщений и постановка их в очередь."""

from datetime import datetime, timedelta, timezone

from ..notify import utf16_len
from . import snapshot as snapshots, spool

# В снимке часового пояса нет: сообщения и панель называют его явно.
ZONE = timezone(timedelta(hours=3))
LIMIT = 4096
CLIENT = {
    'initial': 'Основное VPN-подключение временно недоступно. Аварийный конфиг Hysteria2 для устройства '
               '{device_name} — ниже. Как подключиться: {instructions_url}. Сохраните прежний VPN-профиль — '
               'он понадобится после восстановления',
    'leader_changed': 'Аварийный сервер сменился. Для устройства {device_name} используйте новый конфиг '
                      'Hysteria2 ниже. Предыдущий аварийный профиль больше не используйте. '
                      'Как подключиться: {instructions_url}',
    'recovered': 'Основное VPN-подключение восстановлено. Отключите аварийное подключение в используемом '
                 'приложении и включите прежний профиль WireGuard, OpenVPN или IKEv2. Аварийный доступ '
                 'будет отключён в {deadline}, примерно через 10 минут',
    'reinstalled': 'Основное VPN-подключение восстановлено. Аварийный доступ уже отключён: включите прежний '
                   'профиль WireGuard, OpenVPN или IKEv2 и отключите аварийное подключение в используемом '
                   'приложении',
    'return_cancelled': 'Возврат отменён: основное VPN-подключение снова недоступно. Аварийный доступ '
                        'сохраняется — если вы уже переключились, снова включите аварийное подключение '
                        'Hysteria2',
}
ADMIN = ('Основной VPN недоступен. Аварийная панель запущена на {exit_name}. Адрес: {panel_url}. '
         'Пароль: {panel_password}. Для входа нужен только пароль')
ADMIN_SELF_SIGNED = ('. Сертификат самоподписанный: браузер покажет предупреждение о небезопасном '
                     'подключении. Продолжайте, только если отпечаток SHA-256 совпадает: {fingerprint}')
ADMIN_LEADER_CHANGED = 'Аварийный сервер сменился — новые адрес и пароль. '
ADMIN_RECOVERED = ('Основной VPN восстановлен. Аварийный режим будет завершён в {deadline}, '
                   'примерно через 10 минут')
ADMIN_RETURN_CANCELLED = ('Основной VPN снова недоступен. Завершение аварийного режима отменено, '
                          'аварийная панель и доступ продолжают работать')
ADMIN_ENTRY_MOVED = '. Основной сервер вернулся с новым адресом: {address}'
OUTAGE = 'Entry недоступен. Аварийный режим включится через {minutes} мин'
OUTAGE_BACK = 'Entry снова доступен. Аварийный режим не включался'
OUTAGE_BUTTON = 'Запустить сейчас'
NO_CHANNEL = 'нет канала доставки'
NO_TOKEN = 'автодоставка невозможна: нет токена бота'
# Клиент получил аварийный конфиг и ещё не слышал о восстановлении.
INFORMING = ('initial', 'leader_changed', 'return_cancelled')


def clock(wall):
    return f'{datetime.fromtimestamp(wall, ZONE):%H:%M}'


def footer(exit_name, wall):
    stamp = datetime.fromtimestamp(wall, ZONE)
    offset = f'{stamp:%z}'
    return f'\n\nУзел: {exit_name}. Время выдачи: {stamp:%H:%M %d.%m.%Y} (UTC{offset[:3]}:{offset[3:]})'


def split(text, tail):
    """Части не длиннее предела Telegram в единицах UTF-16, знак не рвётся."""
    room, parts, part = LIMIT - utf16_len(tail), [], ''
    for char in text:
        if utf16_len(part + char) > room:
            parts.append(part)
            part = ''
        part += char
    return [{'method': 'sendMessage', 'text': chunk + tail} for chunk in parts + [part]]


def _denied(client, now):
    if client['expires_at'] == snapshots.iso_time(0):
        return 'неизвестные или конфликтующие права'
    if client['expires_at'] is not None and snapshots.timestamp(client['expires_at']) <= now:
        return 'срок доступа истёк'
    if not client['allowed']:
        return 'клиент заморожен'
    return None


def _config(client, profiles, text, url, tail):
    """Сообщения с конфигами всех устройств клиента или причина отказа: неполный набор не уходит."""
    if not client['devices']:
        return [], 'нет устройств'
    parts = []
    for device in client['devices']:
        uri = profiles.get(device['id'], {}).get('uri')
        if not uri:
            return [], 'нет аварийного профиля устройства'
        parts += split(text.format(device_name=device['name'], instructions_url=f'{url}/instructions'), tail)
        if utf16_len(uri + tail) <= LIMIT:
            parts.append({'method': 'sendMessage', 'text': uri + tail})
        else:
            parts.append({'method': 'sendDocument', 'document': uri, 'caption': tail.strip(),
                          'filename': f"hysteria2-{device['id']}.txt"})
    return parts, None


def clients(directory, image, event, *, emergency, generation, exit_name, wall, version=None,
            profiles=None, url='', text=None, only=None, **fields):
    """Сообщение события каждому клиенту снимка; only - только этим клиентам.

    С profiles каждому уходят конфиги его устройств; text - свободный текст админа.
    """
    tail = footer(exit_name, wall)
    for client in image.get('clients', []):
        if only is not None and client['id'] not in only:
            continue
        parts, reason = [], _denied(client, wall) if profiles is not None or text else None
        if not reason and client['telegram_chat_id'] is None:
            reason = NO_CHANNEL
        if not reason and profiles is not None:
            parts, reason = _config(client, profiles, CLIENT[event], url, tail)
        elif not reason:
            parts = split(text or CLIENT[event].format(**fields), tail)
        if not reason and not image.get('bot_token'):
            reason = NO_TOKEN
        spool.put(directory, f"{emergency}:{event}:{version or generation}:{client['id']}",
                  client['telegram_chat_id'], parts, reason=reason, emergency=emergency,
                  generation=generation, client=client['id'], event=event, audience='client')


def admin(directory, image, event, text, *, emergency, generation, exit_name, wall, version=None):
    chat = image.get('admin_chat_id')
    reason = NO_CHANNEL if chat is None else None if image.get('bot_token') else NO_TOKEN
    spool.put(directory, f'{emergency}:{event}:{version or generation}:admin', chat,
              split(text, footer(exit_name, wall)), reason=reason, emergency=emergency,
              generation=generation, client=None, event=event, audience='admin')


def panel_text(payload, event):
    text = ADMIN.format(**payload)
    if payload.get('certificate_fingerprint'):
        text += ADMIN_SELF_SIGNED.format(fingerprint=payload['certificate_fingerprint'])
    return (ADMIN_LEADER_CHANGED if event == 'leader_changed' else '') + text


def informed(directory, emergency):
    """Клиенты, получившие аварийный конфиг и не слышавшие о восстановлении."""
    known = set()
    for row in spool.rows(directory, emergency=emergency, audience='client', state='sent'):
        if row['event'] in INFORMING:
            known.add(row['client'])
        elif row['event'] in ('recovered', 'reinstalled'):
            known.discard(row['client'])
    return known
