"""wg0.conf так, как его берёт сам wg: решение «принят/отвергнут», интерфейс и пиры.

Один путь для сверки бэкапа (anysda-backup.sh, на узле берёт из /usr/local/lib/anysda)
и проверки трафика (verify-traffic.py, грузит рядом с собой). Файл режется как у
wg-quick и уходит в `wg setconf` на свежем wg0 во временном netns, ответ - `wg show
wg0 dump`. Файл, который wg отвергает, - ValueError с «wg0.conf» и отказами wg;
нет wg, ip или права на netns - ValueError «проверить нечем». Живой wg0 узла не задет.
"""
import base64
import binascii
import itertools
import os
import re
import shutil
import subprocess

# Имена, которые wg-quick забирает себе из секции [Interface] (parse_options).
WG_QUICK = {b'address', b'dns', b'mtu', b'table', b'preup', b'postup', b'predown', b'postdown', b'saveconfig'}
SPACE = b' \t\n\v\f\r'
# Отказ wg без строки файла: какое имя wg0.conf он называет.
NAMED = (('public key', 'PublicKey'), ('AllowedIP', 'AllowedIPs'), ('IP address', 'AllowedIPs'), ('endpoint', 'Endpoint'),
         ('keepalive', 'PersistentKeepalive'), ('Fwmark', 'FwMark'), ('Servname', 'ListenPort'))
_names = itertools.count()


def wg_quick_strip(data):
    """Строки, которые wg-quick (parse_options, bash `read -r`) отдаёт wg: [(номер строки файла, байты)].

    NUL bash не читает, края строки `read` обрезает, имена WG_QUICK после заголовка
    `[Interface]` (регистр любой) уходят wg-quick; SaveConfig не true/false он отвергает.
    """
    kept, inside = [], False
    lines = data.split(b'\n')
    if lines and lines[-1] == b'':
        lines.pop()
    for number, raw in enumerate(lines, 1):
        line = raw.replace(b'\0', b'').strip(b' \t')
        stripped = line.split(b'#', 1)[0]
        key, _, value = stripped.partition(b'=')
        key = key.strip(SPACE).lower()
        if key.startswith(b'['):
            inside = key == b'[interface]'
        if inside and key in WG_QUICK:
            if key == b'saveconfig' and value.strip(SPACE).lower() not in (b'true', b'false'):
                raise ValueError(f'wg0.conf: строка {number} SaveConfig не true и не false, wg-quick отвергает файл')
            continue
        kept.append((number, line))
    return kept


def _clean(line):
    """Строка, как её видит wg: до первого `#`, без пробельных символов."""
    return re.sub(rb'[ \t\n\v\f\r]', b'', line.split(b'#', 1)[0])


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
    cleaned = [_clean(line) for _, line in lines]
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


def wg_dump(data):
    """`wg show wg0 dump` после `wg setconf` файла `data` (bytes или str) на свежем wg0 во временном netns."""
    if isinstance(data, str):
        data = data.encode()
    lines = wg_quick_strip(data)
    if not shutil.which('wg') or not shutil.which('ip'):
        raise ValueError('wg0.conf: проверить нечем - нет wg или ip')
    name = f'vpn2wgc{os.getpid()}x{next(_names)}'

    def call(*args, data=None):
        return subprocess.run(args, input=data, capture_output=True, timeout=30, check=False)

    done = call('ip', 'netns', 'add', name)
    if done.returncode:
        raise ValueError(f'wg0.conf: проверить нечем - ip netns add: {_said(done)}')
    try:
        done = call('ip', '-n', name, 'link', 'add', 'wg0', 'type', 'wireguard')
        if done.returncode:
            raise ValueError(f'wg0.conf: проверить нечем - wg0 в netns: {_said(done)}')
        refusals = []
        # /usr/bin/wg под AppArmor не читает /tmp и /run: тело только через stdin.
        # На отказ wg строка, которую он назвал, гасится, и wg спрашивается снова - так видны все отказы.
        while True:
            done = call('ip', 'netns', 'exec', name, 'wg', 'setconf', 'wg0', '/dev/stdin',
                        data=b''.join(line + b'\n' for _, line in lines))
            if not done.returncode:
                break
            if not done.stderr.rstrip().endswith((b'Configuration parsing error', b'Invalid configuration')):
                raise ValueError(f'wg0.conf: проверить нечем - wg setconf: {_said(done)}')
            refusal, found = _refusal(done.stderr, lines)
            refusals.append(refusal)
            if found is None:
                break
            lines[found] = (lines[found][0], b'')
        if refusals:
            raise ValueError('wg0.conf: wg отвергает файл: ' + '; '.join(refusals))
        done = call('ip', 'netns', 'exec', name, 'wg', 'show', 'wg0', 'dump')
        if done.returncode:
            raise ValueError(f'wg0.conf: проверить нечем - wg show: {_said(done)}')
        dump = done.stdout.decode().splitlines()
    finally:
        call('ip', 'netns', 'del', name)
    # wg печатает ключ прижатым; в ответ идёт ключ из строки файла, который wg взял.
    private = dump[0].split('\t', 1)[0]
    taken = [clean.partition(b'=')[2].decode('latin-1') for clean in map(_clean, (line for _, line in lines))
             if clean.partition(b'=')[0].lower() == b'privatekey']
    taken = [key for key in taken if _clamped(key) == private]
    if taken:
        dump[0] = taken[-1] + dump[0][len(private):]
    return dump


def read(data):
    """Интерфейс и пиры, какими их держит wg после setconf.

    Интерфейс - {имя wg0.conf: значение} только заданных: PrivateKey, ListenPort, FwMark.
    Пиры в порядке dump: ключ, PSK ('' без него), AllowedIPs.
    """
    dump = wg_dump(data)
    private, _public, port, fwmark = dump[0].split('\t')[:4]
    interface = {name: value for name, value, unset in
                 (('PrivateKey', private, '(none)'), ('ListenPort', port, '0'), ('FwMark', fwmark, 'off'))
                 if value != unset}
    peers = []
    for line in dump[1:]:
        key, psk, _endpoint, allowed = line.split('\t')[:4]
        peers.append({'key': key, 'preshared_key': '' if psk == '(none)' else psk,
                      'allowed': [] if allowed == '(none)' else allowed.split(',')})
    return interface, peers


def conf_peers(data):
    """Пиры wg0.conf, какими их держит wg: ключ, PSK, AllowedIPs."""
    return read(data)[1]
