"""wg0.conf: разбор текста так, как его читает wg и wg-quick (stdlib only).

Чистые функции без внешних программ: проверка ключа, нарезка на шапку и пиры,
проверка файла перед установкой, тело для `wg syncconf`, ключи и адреса.
Файл читается как latin-1, один символ на байт, и пишется обратно так же
(`vpn2.fsutil.atomic(..., encoding='latin-1')`): байты не меняются.
"""
import base64
import re

# Канонический ключ wg: 32 байта base64, последний знак не несёт лишних битов.
KEY = r'[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]='
# Пробельные символы C isspace() внутри строки: их wg пропускает. \s и str.strip()
# Python берут больше (\x1c, \x85, NBSP...), а wg их читает как текст.
BLANK = r'[ \t\v\f\r]'
# То же для одной строки файла, у которой уже нет \n.
SPACE = ' \t\n\v\f\r'
# \s берёт последним U+3000; в latin-1 тексте каждый такой символ виден и как UTF-8 байты.
FOREIGN_SPACE = '|'.join(sorted({re.escape(view) for code in range(0x3001) if chr(code).isspace()
                                 for view in (chr(code), chr(code).encode().decode('latin-1'))},
                                key=len, reverse=True))
# Ключи, которые получает wg syncconf, и значения, какие wg читает, когда BLANK убран.
# Имена секций и ключей wg сравнивает в любом ASCII-регистре (strncasecmp).
WG_ADDRESS = '-?[0-9A-Fa-f.:]+(?:/[0-9]+)?'
WG_VALUES = {'PrivateKey': KEY, 'ListenPort': '[0-9]+', 'FwMark': '(?:(?ai:off)|0x[0-9A-Fa-f]+|[0-9]+)',
             'PublicKey': KEY, 'PresharedKey': KEY, 'AllowedIPs': f'{WG_ADDRESS}(?:,{WG_ADDRESS})*'}
WG_SECTIONS = {'[Interface]': ('PrivateKey', 'ListenPort', 'FwMark'),
               '[Peer]': ('PublicKey', 'PresharedKey', 'AllowedIPs')}
# Ключи [Interface], которые wg-quick забирает себе до wg: имя без краевых пробелов, любой регистр.
WG_QUICK_KEYS = ('Address', 'DNS', 'MTU', 'Table', 'PreUp', 'PreDown', 'PostUp', 'PostDown', 'SaveConfig')
WG_QUICK = {name.lower().encode() for name in WG_QUICK_KEYS}
CONF_PATH = '/etc/wireguard/wg0.conf'


class WgError(ValueError):
    """Файл или ключ, который wg не берёт."""


class WgUnchecked(WgError):
    """Проверить нечем: нет wg, ip, unshare или setpriv, таймаут, сбой шага netns."""


def valid_key(value, *, strict=True, nonzero=False):
    """Ключ wg: 43 знака base64 и `=`.

    strict - канонический (последний знак без лишних битов), как у wg. strict=False -
    только форма {43}=. nonzero - ещё 32 байта, не нулевых, и канонический вид.
    """
    if not isinstance(value, str):
        return False
    if not re.fullmatch(KEY if strict else r'[A-Za-z0-9+/]{43}=', value):
        return False
    if nonzero:
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError:
            return False
        return len(raw) == 32 and any(raw) and base64.b64encode(raw).decode() == value
    return True


def _lower(text):
    """Имя в нижнем регистре только для ASCII: K (Кельвин) и кириллица остаются собой."""
    return text.encode().lower().decode()


def quick_strip(data):
    """Строки, которые wg-quick (parse_options, bash `read -r`) отдаёт wg: [(номер строки, байты)].

    NUL bash не читает, края строки `read` обрезает, имена wg-quick после заголовка
    `[Interface]` (регистр любой) уходят wg-quick; SaveConfig не true/false он отвергает.
    """
    kept, inside = [], False
    lines = data.split(b'\n')
    if lines and lines[-1] == b'':
        lines.pop()
    for number, raw in enumerate(lines, 1):
        text = raw.replace(b'\0', b'').strip(b' \t')
        stripped = text.split(b'#', 1)[0]
        key, _, value = stripped.partition(b'=')
        key = key.strip(SPACE.encode()).lower()
        if key.startswith(b'['):
            inside = key == b'[interface]'
        if inside and key in WG_QUICK:
            if key == b'saveconfig' and value.strip(SPACE.encode()).lower() not in (b'true', b'false'):
                raise WgError(f'wg0.conf: строка {number} SaveConfig не true и не false, wg-quick отвергает файл')
            continue
        kept.append((number, text))
    return kept


def line(text):
    """Секция или ключ syncconf, который wg читает в строке (None, если ни то ни другое), и значение.

    wg режет строку по первому #, убирает BLANK везде и сверяет имя в ASCII-регистре
    (wireguard-tools config.c: config_read_line, process_line). Не распознана - (None, строка).
    """
    seen = re.sub(BLANK, '', text.split('#', 1)[0])
    for name in WG_SECTIONS:
        if re.fullmatch(re.escape(name), seen, re.I | re.A):
            return name, ''
    for name in WG_VALUES:
        if re.match(name + '=', seen, re.I | re.A):
            return name, seen[len(name) + 1:]
    return None, seen


def split(text):
    """Шапка ([Interface], своя у развёртывания) и хвост пиров (свой у доступа)."""
    lines = text.split('\n')
    cut = next((index for index, item in enumerate(lines) if line(item)[0] == '[Peer]'), None)
    if cut is None:
        return text.rstrip(SPACE) + '\n', ''
    # Комментарии вплотную над первым [Peer] - подпись этого пира, уходят с хвостом.
    while cut and line(lines[cut - 1]) == (None, ''):
        cut -= 1
    while not re.sub(BLANK, '', lines[cut]):
        cut += 1
    return '\n'.join(lines[:cut]).rstrip(SPACE) + '\n', '\n'.join(lines[cut:])


def check(text):
    """Файл wg0.conf годен для установки: каждая секция и ключ читаются так же, как их читает wg.

    WgError с текстом отказа, если нет: секция или ключ не на месте, значение не читается,
    [Interface] после [Peer], не один PublicKey у пира, нет ListenPort или PrivateKey.
    """
    section, quick, public, ports = None, False, [], []
    for item in text.split('\n'):
        name, value = line(item)
        # Секция wg-quick: любая строка, чей обрезанный ключ начинается с [, закрывает её,
        # а открывает ту, что он вырезает, только [Interface] в любом регистре.
        key = item.split('#', 1)[0].split('=', 1)[0].strip(' \t\v\f\r')
        quick = _lower(key) == '[interface]' if key.startswith('[') else quick
        if name in WG_SECTIONS:
            # wg читает и второй [Interface], но split() держит шапкой только первый:
            # ключи под вторым остались бы в хвосте пиров и перекрыли бы новую шапку.
            if section == '[Peer]' and name == '[Interface]':
                raise WgError(f'{CONF_PATH}: {item!a} after [Peer] number {len(public)}; '
                              'move the keys under it into the [Interface] of the config you install '
                              'and delete that section from the file')
            section = name
            public += [0] if name == '[Peer]' else []
            continue
        if not name:
            # Строка, которую \s Python читает как ключ syncconf, wg должен читать так же.
            if line(re.sub(FOREIGN_SPACE, '', item))[0]:
                raise WgError(f'wg cannot read {item!a}')
            # wg-quick read_bool: значение после первого =, до #, без краёв, - true или false.
            saved = item.split('#', 1)[0].partition('=')[2].strip(SPACE).lower()
            if quick and _lower(key) == 'saveconfig' and saved not in ('true', 'false'):
                raise WgError(f'SaveConfig: wg-quick takes true or false, not {item!a}')
            # Любая другая строка в шапке останавливает wg-quick up («Line unrecognized»).
            # Хвост пиров - живой файл, здесь не проверяется.
            if value and section != '[Peer]' and not (
                    quick and re.fullmatch('|'.join(WG_QUICK_KEYS), key, re.I | re.A)):
                raise WgError(f'wg cannot read {item!a}')
            continue
        if name not in WG_SECTIONS.get(section, ()):
            raise WgError(f'{name} in {section or "no section"}: wg cannot read {item!a}')
        if not re.fullmatch(WG_VALUES[name], value):
            raise WgError(f'{name}: wg cannot read {item!a}')
        # wg parse_fwmark(): strtoul() по основанию 16 после 0x в нижнем регистре, иначе 10.
        if name == 'FwMark' and value.lower() != 'off' and \
                int(value, 16 if value.startswith('0x') else 10) > 0xffffffff:
            raise WgError(f'FwMark: wg takes 0/off or 0-0xffffffff, not {item!a}')
        if name == 'PublicKey':
            public[-1] += 1
        ports += [int(value)] if name == 'ListenPort' else []
    if not any(line(item)[0] == 'PrivateKey' for item in text.split('\n')):
        raise WgError('[Interface] PrivateKey missing')
    if not ports or not all(0 < port < 65536 for port in ports):
        raise WgError('[Interface] ListenPort missing')
    if any(count != 1 for count in public):
        raise WgError('[Peer] without one valid PublicKey')


def syncconf_body(text):
    """Что отдать `wg syncconf wg0 /dev/stdin`: только ключи wg, строки как написаны (с комментариями).

    Address и прочее - wg-quick, их тут нет. Строки делятся только по \\n, как у wg.
    Возвращает тело целиком, с завершающим \\n.
    """
    header, peers = split(text)
    body = '\n'.join(item for item in header.split('\n')
                     if line(item)[0] in ('[Interface]', *WG_SECTIONS['[Interface]'])) + '\n' + \
        '\n'.join(item for item in peers.split('\n') if line(item)[0] in ('[Peer]', *WG_SECTIONS['[Peer]']))
    return body + '\n'


def _pairs(text):
    """(имя, значение) каждой строки как их видят wg-quick и wg: NUL вон, до первого #, без пробелов."""
    return [re.sub(r'[ \t\n\v\f\r]', '', item.replace('\0', '').split('#', 1)[0]).partition('=')
            for item in text.split('\n')]


def private_key(text, *, interface_only=True):
    """Приватный ключ из wg0.conf (последний) или None.

    interface_only=True - только в [Interface] (awk в 28-wireguard.sh и anysda-restore.sh);
    False - в любой секции (restore.wg_key). Ключ в [Peer] при True не берётся.
    Пустое значение последней строки в режиме True сбрасывает ключ, как в awk.
    """
    if not interface_only:
        keys = [value for name, eq, value in _pairs(text) if eq and value and _lower(name) == 'privatekey']
        return keys[-1] if keys else None
    key, inside = '', False
    for item in text.replace('\0', '').split('\n'):
        clean = re.sub(r'[ \t\r\v\f]', '', item.split('#', 1)[0])
        if clean.startswith('['):
            inside = _lower(clean) == '[interface]'
            continue
        if inside and _lower(clean).startswith('privatekey='):
            key = clean.partition('=')[2]
    return key or None


def public_keys(text):
    """Множество PublicKey всех пиров (любая секция, любой регистр имени, пустые не в счёт)."""
    return {value for name, eq, value in _pairs(text) if eq and value and _lower(name) == 'publickey'}


def drop_peers(text, keys):
    """text без пиров, чей (последний) PublicKey в keys; остальные строки байт в байт (latin-1 текст).

    Комментарии вплотную над секцией - её подпись, уходят с ней; пустые строки и комментарии
    в конце файла остаются на месте.
    """
    def detach_notes(block):
        n = len(block)
        while n and not block[n - 1][0] and '#' in block[n - 1][2]:
            n -= 1
        notes = block[n:]
        del block[n:]
        return notes

    blocks = [[]]
    for row in re.findall(r'[^\n]*\n|[^\n]+', text):
        clean = re.sub(r'[ \t\n\v\f\r]', '', row.replace('\0', '').split('#', 1)[0])
        if clean.startswith('['):
            blocks.append(detach_notes(blocks[-1]))
        blocks[-1].append((_lower(clean), clean.partition('=')[2], row))
    blocks.append(detach_notes(blocks[-1]))
    kept = []
    for block in blocks:
        found = [value for name, value, _ in block if name.startswith('publickey=')]
        names = [name for name, _, _ in block if name]
        if names and names[0] == '[peer]' and found and found[-1] in keys:
            continue
        kept += [row for _, _, row in block]
    return ''.join(kept)


def addresses(text):
    """Address из [Interface] по правилам wg-quick, в порядке файла.

    Строка до первого #, имя и значение без краёв ( \\t\\r\\v\\f), имя в любом регистре,
    значение делится по запятым и пробелам. Сравнение с живым интерфейсом - у вызывающего.
    """
    found, inside = [], False
    for item in text.split('\n'):
        item = item.split('#', 1)[0]
        key = item.partition('=')[0].strip(' \t\r\v\f')
        value = item.partition('=')[2].strip(' \t\r\v\f')
        if key.startswith('['):
            inside = _lower(key) == '[interface]'
            continue
        if inside and _lower(key) == 'address' and '=' in item:
            found += [part for part in re.split(r'[, \t]+', value) if part]
    return found


def profile(text):
    """Клиентский профиль (wgcf): {private_key, address, public_key, endpoint}; нет - None.

    Имена в любом регистре, пробелы вокруг `=` любые или никаких, до первого #. Ключи берутся
    последние, address - первый IPv4 без маски, endpoint - (host, port) по последнему `:`.
    """
    found = {}
    for row in text.split('\n'):
        name, eq, value = row.split('#', 1)[0].partition('=')
        if eq:
            found.setdefault(_lower(name.strip(SPACE)), []).append(value.strip(SPACE))
    address = next((item.partition('/')[0] for value in found.get('address', [])
                    for item in re.split(r'[, \t]+', value) if item and ':' not in item), None)
    endpoint = None
    if found.get('endpoint'):
        host, _, port = found['endpoint'][-1].replace(' ', '').rpartition(':')
        endpoint = (host, port)
    last = {name: found[name][-1] for name in ('privatekey', 'publickey') if name in found}
    return {'private_key': last.get('privatekey'), 'address': address,
            'public_key': last.get('publickey'), 'endpoint': endpoint}


# Дальше (шаг 2): run, genkey, genpsk, pubkey, render_interface, render_peers, peer_addresses,
# dump, read, conf_peers, parse_dump, allowed_ips, preshared_keys, transfer.
