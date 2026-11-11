"""wg0.conf: разбор текста так, как его читает wg и wg-quick (stdlib only).

Чистые функции без внешних программ: проверка ключа, нарезка на шапку и пиры,
проверка файла перед установкой, тело для `wg syncconf`, ключи и адреса.
Файл читается как latin-1, один символ на байт, и пишется обратно так же
(`vpn2.fsutil.atomic(..., encoding='latin-1')`): байты не меняются.
"""
import base64
import binascii
import re
import shutil
import subprocess

# Канонический ключ wg: 32 байта base64, последний знак не несёт лишних битов.
KEY = r'[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]='
# Пробельные символы C isspace() внутри строки: их wg пропускает. \s и str.strip()
# Python берут больше (\x1c, \x85, NBSP...), а wg их читает как текст.
BLANK = r'[ \t\v\f\r]'
# То же для одной строки файла, у которой уже нет \n.
SPACE = ' \t\n\v\f\r'
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


def _pairs(text):
    """(имя, значение) каждой строки как их видят wg-quick и wg: NUL вон, до первого #, без пробелов."""
    return [re.sub(r'[ \t\n\v\f\r]', '', item.replace('\0', '').split('#', 1)[0]).partition('=')
            for item in text.split('\n')]


def private_key(text, *, interface_only=True):
    """Приватный ключ из wg0.conf (последний) или None.

    interface_only=True - только в [Interface] (роль wireguard и anysda-restore.sh);
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




# Шаг 2: внешний wg, рендер и вывод `wg show`.

# wg0 в анонимном netns: netns держит только этот процесс и умирает вместе с ним.
# --kill-child и pdeathsig - убитая проверка (SIGTERM, SIGKILL) не оставляет wg0 с ключами.
# Код выхода называет шаг, на котором не вышло.
ASK = ('setpriv', '--pdeathsig', 'KILL', 'unshare', '--net', '--pid', '--fork', '--kill-child', 'sh', '-c',
       'ip link add wg0 type wireguard || exit 120; wg setconf wg0 /dev/stdin || exit 121; '
       'wg show wg0 dump || exit 122')
STEPS = {120: 'wg0 в netns', 121: 'wg setconf', 122: 'wg show'}
TIMEOUT = 30
# Отказ wg без строки файла: какое имя wg0.conf он называет.
NAMED = (('public key', 'PublicKey'), ('AllowedIP', 'AllowedIPs'), ('IP address', 'AllowedIPs'),
         ('endpoint', 'Endpoint'), ('keepalive', 'PersistentKeepalive'), ('Fwmark', 'FwMark'),
         ('Servname', 'ListenPort'))


def run(argv, *, data=None, timeout=TIMEOUT):
    """Запуск программы: bytes на входе (str кодируется) и на выходе, код не проверяется."""
    if isinstance(data, str):
        data = data.encode()
    return subprocess.run(list(argv), input=data, capture_output=True, timeout=timeout, check=False)


_DEFAULT_RUN = run


def _call(argv, runner, *, data=None, timeout=TIMEOUT, name=None):
    """runner(argv): нет программы и таймаут - WgUnchecked, без traceback и без argv в тексте."""
    label = name or argv[0]
    try:
        return runner(argv, data=data, timeout=timeout)
    except FileNotFoundError:
        raise WgUnchecked(f'wg0.conf: проверить нечем - нет {argv[0]}') from None
    except subprocess.TimeoutExpired:
        raise WgUnchecked(f'wg0.conf: проверить нечем - {label} не ответил за {timeout} с') from None


def _tool(argv, runner, *, data=None):
    """Вывод `wg genkey|genpsk|pubkey` без пробелов по краям; отказ - WgError без ключа в тексте."""
    done = _call(argv, runner, data=data)
    if done.returncode:
        raise WgError(f'{argv[0]} {argv[1]}: код {done.returncode}')
    out = done.stdout.decode(errors='replace').strip()
    if not valid_key(out, strict=False):
        raise WgError(f'{argv[0]} {argv[1]}: ответ не ключ')
    return out


def genkey(*, run=run):
    """Новый приватный ключ (`wg genkey`)."""
    return _tool(['wg', 'genkey'], run)


def genpsk(*, run=run):
    """Новый общий ключ (`wg genpsk`)."""
    return _tool(['wg', 'genpsk'], run)


def pubkey(private, *, run=run):
    """Публичный ключ по приватному; приватный идёт через stdin, не в argv и не в текст ошибки."""
    return _tool(['wg', 'pubkey'], run, data=private + '\n')


def render_interface(addresses, port, private, *, note=None):
    """Начальная шапка wg0.conf (роль wireguard): [комментарий], Address, ListenPort, PrivateKey."""
    return (f'# {note}\n' if note is not None else '') + \
        f'[Interface]\nAddress = {", ".join(addresses)}\nListenPort = {port}\nPrivateKey = {private}\n'


def render_peers(peers, *, omit_empty=False):
    """Пиры [{key, preshared_key, allowed}]: каждый с пустой строки перед [Peer].

    omit_empty=False (панель) пишет PresharedKey и AllowedIPs всегда; True (проверка
    трафика) опускает пустые: пустое значение wg отвергает.
    """
    out = ''
    for peer in peers:
        out += f'\n[Peer]\nPublicKey = {peer["key"]}\n'
        if peer['preshared_key'] or not omit_empty:
            out += f'PresharedKey = {peer["preshared_key"]}\n'
        if peer['allowed'] or not omit_empty:
            out += f'AllowedIPs = {", ".join(peer["allowed"])}\n'
    return out


def peer_addresses(ip):
    """Адреса пира в профилях клиентов: ip/32 и fd66:66::N/128 (десятичные октеты в ULA, как у панели)."""
    subnet, host = ip.split('.')[2:]
    suffix = host if subnet == '66' else subnet + ':' + host
    return [ip + '/32', 'fd66:66::' + suffix + '/128']


def _clean(row):
    """Строка, как её видит wg: до первого `#`, без пробельных символов."""
    return re.sub(rb'[ \t\n\v\f\r]', b'', row.split(b'#', 1)[0])


def _said(done):
    return done.stderr.decode(errors='replace').strip().replace('\n', '; ')


def _refusal(stderr, lines):
    """Первый отказ wg и индекс его строки в `lines` (или None).

    Значение, которое wg повторил, печатается, только если это не ключ и не вся
    нераспознанная строка (в ней может быть ключ): имя строки - если оно из букв.
    """
    reason = stderr.decode('latin-1').split('\n', 1)[0]
    text, _, echoed = reason.partition(': `')
    echoed = echoed.removesuffix("'").encode('latin-1')
    secret = 'Key' in text or text.startswith('Line unrecognized')
    value = f' `{echoed.decode("latin-1")}`' if echoed and not secret else ''
    cleaned = [_clean(row) for _, row in lines]
    found = None
    if echoed:
        found = next((i for i, clean in enumerate(cleaned) if echoed in (clean, clean.partition(b'=')[2])), None)
        if found is None:
            found = next((i for i, clean in enumerate(cleaned) if echoed in clean.partition(b'=')[2]), None)
    if found is None:
        name = next((name for marker, name in NAMED if marker in text), '')
        return (f'{name}: ' if name else '') + text + value, None
    name = cleaned[found].partition(b'=')[0].decode('latin-1')
    shown = f' `{name}`' if re.fullmatch(r'\[?[A-Za-z]{1,32}\]?', name) else ''
    if name.lower().endswith('key'):
        value = ''
    return f'строка {lines[found][0]}{shown}: {text}{value}', found


def _clamped(key):
    """Приватный ключ base64 в той форме, в какой его держит и печатает wg (curve25519 clamp), или None."""
    try:
        raw = bytearray(base64.b64decode(key, validate=True))
    except (binascii.Error, ValueError):
        return None
    if len(raw) != 32:
        return None
    raw[0] &= 248
    raw[31] = (raw[31] & 127) | 64
    return base64.b64encode(raw).decode()


def dump(data, *, run=run, timeout=TIMEOUT, which=shutil.which):
    """`wg show wg0 dump` после `wg setconf` файла `data` (bytes или str) на свежем wg0 в анонимном netns.

    WgError «wg отвергает файл» с отказами wg; WgUnchecked «проверить нечем»: нет wg, ip, unshare,
    setpriv, таймаут, сбой шага netns. which проверяет программы заранее только у настоящего run.
    """
    if isinstance(data, str):
        data = data.encode()
    lines = quick_strip(data)
    missing = [name for name in ('wg', 'ip', 'unshare', 'setpriv') if not which(name)] \
        if run is _DEFAULT_RUN else []
    if missing:
        raise WgUnchecked('wg0.conf: проверить нечем - нет ' + ', '.join(missing))
    refusals = []
    # /usr/bin/wg под AppArmor не читает /tmp и /run: тело только через stdin.
    # На отказ wg строка, которую он назвал, гасится, и wg спрашивается снова - так видны все отказы.
    while True:
        done = _call(ASK, run, data=b''.join(row + b'\n' for _, row in lines), timeout=timeout, name='wg')
        if not done.returncode:
            break
        if done.returncode != 121 or not done.stderr.rstrip().endswith(
                (b'Configuration parsing error', b'Invalid configuration')):
            step = STEPS.get(done.returncode, 'unshare --net')
            raise WgUnchecked(f'wg0.conf: проверить нечем - {step}: {_said(done)}')
        refusal, found = _refusal(done.stderr, lines)
        refusals.append(refusal)
        if found is None:
            break
        lines[found] = (lines[found][0], b'')
    if refusals:
        raise WgError('wg0.conf: wg отвергает файл: ' + '; '.join(refusals))
    rows = done.stdout.decode().splitlines()
    # wg печатает ключ прижатым; в ответ идёт ключ из строки файла, который wg взял.
    private = rows[0].split('\t', 1)[0]
    taken = [clean.partition(b'=')[2].decode('latin-1') for clean in (_clean(row) for _, row in lines)
             if clean.partition(b'=')[0].lower() == b'privatekey']
    taken = [key for key in taken if _clamped(key) == private]
    if taken:
        rows[0] = taken[-1] + rows[0][len(private):]
    return rows


def read(data, *, run=run):
    """Интерфейс и пиры, какими их держит wg после setconf.

    Интерфейс - {имя wg0.conf: значение} только заданных: PrivateKey, ListenPort, FwMark.
    Пиры в порядке dump: ключ, PSK ('' без него), AllowedIPs.
    """
    rows = dump(data, run=run)
    private, _public, port, fwmark = rows[0].split('\t')[:4]
    interface = {name: value for name, value, unset in
                 (('PrivateKey', private, '(none)'), ('ListenPort', port, '0'), ('FwMark', fwmark, 'off'))
                 if value != unset}
    peers = []
    for row in rows[1:]:
        key, psk, _endpoint, allowed = row.split('\t')[:4]
        peers.append({'key': key, 'preshared_key': '' if psk == '(none)' else psk,
                      'allowed': [] if allowed == '(none)' else allowed.split(',')})
    return interface, peers


def conf_peers(data, *, run=run):
    """Пиры wg0.conf, какими их держит wg: ключ, PSK, AllowedIPs."""
    return read(data, run=run)[1]


def _table(text, parts):
    """Строки `wg show wg0 <поле>` по табуляции; чужая форма и пустая строка - WgError.

    Как в панели: у двухполевых хвост после первой табуляции остаётся значением, у трёхполевых полей ровно три.
    """
    rows = [row.split('\t', 1) if parts == 2 else row.split('\t') for row in text.splitlines()]
    if any(len(row) != parts for row in rows):
        raise WgError(f'wg show: строка не из {parts} полей')
    return rows


def allowed_ips(text):
    """`wg show wg0 allowed-ips` -> {ключ: отсортированные адреса}."""
    return {key: sorted(addresses.replace(',', ' ').split()) for key, addresses in _table(text, 2)}


def preshared_keys(text):
    """`wg show wg0 preshared-keys` -> {ключ: PSK}."""
    return dict(_table(text, 2))


def transfer(text):
    """`wg show wg0 transfer` -> [{key, rx, tx}]; rx и tx переставлены (вид клиента), как у панели."""
    rows = _table(text, 3)
    try:
        return [{'key': key, 'rx': int(tx), 'tx': int(rx)} for key, rx, tx in rows]
    except ValueError as error:
        raise WgError(f'wg show: счётчик не число: {error}') from None
