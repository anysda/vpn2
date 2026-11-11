"""Экзит: профиль WARP, секреты каналов, сертификат и конфиг sing-box.

Модуль не тянет PyYAML и сторонних пакетов: роль exit зовёт его системным
python прямо из колеса.
"""
import base64
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

from vpn2.watchdog import hy2_bandwidth

WGCF = '/usr/local/bin/wgcf'
SING_BOX = '/usr/local/bin/sing-box'
WARP_API = 'https://api.cloudflareclient.com/v0a5641/reg/'
# С этими значениями sing-box стартует и direct-каналы работают, а WARP на
# узле мёртв: сторож видит loopback-адрес пира и считает WARP отключённым.
WARP_PLACEHOLDER = {
    'WGCF_PRIVKEY': 'wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0=',
    'WGCF_LOCAL_IPV4': '10.111.111.1',
    'WGCF_PEER_PUBKEY': 'wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0=',
    'WGCF_PEER_ENDPOINT_HOST': '127.0.0.1',
    'WGCF_PEER_ENDPOINT_PORT': '2408',
    'WGCF_RESERVED_JSON': '[0,0,0]',
}
WARP_PEER_FALLBACK = '162.159.192.1'


def _output(argv, run, **kwargs):
    return run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)


def _register(etc, say, run):
    """Вывод register несёт Id аккаунта и устройства: в лог идёт только хвост сбоя."""
    try:
        # Без таймаута register при фильтрации Cloudflare висит ~145 с на TCP connect.
        done = _output([WGCF, 'register', '--accept-tos'], run, cwd=etc, timeout=15)
    except subprocess.TimeoutExpired:
        return False
    if done.returncode == 0:
        return True
    for line in done.stdout.splitlines()[-3:]:
        say(line)
    return False


def _generate(etc, run):
    """0 - профиль построен; 2 - аккаунт от wgcf < 2.3 без key_type; 1 - иной сбой."""
    done = _output([WGCF, 'generate'], run, cwd=etc)
    if done.returncode == 0:
        return 0
    return 2 if 'key_type' in done.stdout else 1


def _protect(etc):
    for name in ('wgcf-account.toml', 'wgcf-profile.conf'):
        (etc / name).chmod(0o600)


def _nonempty(path):
    return path.is_file() and path.stat().st_size > 0


def ensure_warp_profile(etc, say, run=subprocess.run):
    """Регистрирует устройство WARP и строит профиль; False - WARP на узле нет."""
    account, profile = etc / 'wgcf-account.toml', etc / 'wgcf-profile.conf'
    no_api = '⚠ wgcf register не прошёл (Cloudflare API недоступен)'
    if not account.is_file():
        say('регистрирую WARP устройство')
        if not _register(etc, say, run):
            say(f'{no_api} — WARP outbound будет placeholder')
            return False
    if not account.is_file():
        return profile.is_file()
    status = _generate(etc, run)
    if status == 0:
        _protect(etc)
    elif status == 2:
        # Аккаунт без key_type несовместим с wgcf 2.3: создаём устройство нового
        # формата, ключ WARP в конфиге sing-box сменится свежим профилем.
        old = etc / 'wgcf-account.toml.old'
        account.replace(old)
        say('регистрирую WARP устройство заново')
        if _register(etc, say, run):
            if _generate(etc, run) != 0:
                say('⚠ wgcf generate не прошёл после регистрации — WARP outbound будет placeholder')
                return False
            _protect(etc)
        elif _nonempty(profile):
            # Аккаунт возвращаем на место: иначе следующий прогон пойдёт по ветке
            # первой регистрации и при новом сбое потеряет рабочий профиль.
            old.replace(account)
            say(f'{no_api} — оставляю профиль прошлого прогона')
        else:
            say(f'{no_api} — WARP outbound будет placeholder')
            return False
    elif _nonempty(profile):
        say('⚠ wgcf generate не прошёл — оставляю профиль прошлого прогона')
    else:
        say('⚠⚠ wgcf generate не прошёл, профиля нет — WARP outbound будет placeholder, '
            '-warp на этом экзите мёртв')
        return False
    return profile.is_file()


def _field(text, key, *, first=False):
    """Значение `Key = value` из профиля WireGuard; при повторах - как у awk."""
    found = [line.split(' = ', 1)[1] for line in text.splitlines()
             if line.startswith(key) and ' = ' in line]
    if first:
        found = found[:1]
    return '\n'.join(found).replace('\r', '')


def _toml_value(text, key):
    for line in text.splitlines():
        if line.startswith(key) and '=' in line:
            return re.sub(r'["\' ]', '', line.split('=', 1)[1])
    return ''


def _fetch_client_id(device, token, run):
    """client_id устройства из API регистрации; токен идёт в curl через stdin."""
    done = run(['curl', '-fsS', '--max-time', '15', '-H', '@-', WARP_API + device],
               input=f'Authorization: Bearer {token}\n', text=True,
               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        return str(json.loads(done.stdout)['config']['client_id']) if done.returncode == 0 else ''
    except (ValueError, KeyError, TypeError):
        return ''


def warp_reserved(etc, say, run=subprocess.run):
    """reserved = client_id устройства WARP (3 байта), как у официального клиента.

    wgcf его не сохраняет: берём из API и кладём рядом с аккаунтом вместе с
    device_id. Новый аккаунт - новый запрос. API недоступен - reserved нулевой,
    следующий прогон повторит.
    """
    account = (etc / 'wgcf-account.toml').read_text()
    device = _toml_value(account, 'device_id')
    known = etc / 'wgcf-client-id'
    client_id = ''
    if known.is_file():
        for line in known.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == device:
                client_id = parts[1]
    if not client_id:
        client_id = _fetch_client_id(device, _toml_value(account, 'access_token'), run)
        if client_id:
            known.write_text(f'{device} {client_id}\n')
            known.chmod(0o600)
        else:
            say('⚠ client_id WARP из API не получен — reserved нулевой до следующего прогона')
    try:
        raw = base64.b64decode(client_id)[:3]
    except ValueError:
        raw = b''
    return '[%d,%d,%d]' % tuple(raw) if len(raw) == 3 else '[0,0,0]'


def warp_values(etc, say, run=subprocess.run):
    """Переменные WGCF_* шаблона sing-box: из профиля либо placeholder."""
    etc = Path(etc)
    if not ensure_warp_profile(etc, say, run):
        say('wgcf: placeholder (WARP outbound disabled on this node)')
        return dict(WARP_PLACEHOLDER)
    profile = (etc / 'wgcf-profile.conf').read_text()
    endpoint = _field(profile, 'Endpoint').replace(' ', '')
    values = {
        'WGCF_PRIVKEY': _field(profile, 'PrivateKey'),
        'WGCF_LOCAL_IPV4': _field(profile, 'Address', first=True).split(',')[0]
                           .split('/')[0].replace(' ', ''),
        'WGCF_PEER_PUBKEY': _field(profile, 'PublicKey'),
        'WGCF_PEER_ENDPOINT_HOST': endpoint.rsplit(':', 1)[0],
        'WGCF_PEER_ENDPOINT_PORT': endpoint.rsplit(':', 1)[-1],
        'WGCF_RESERVED_JSON': warp_reserved(etc, say, run),
    }
    say(f"wgcf: local={values['WGCF_LOCAL_IPV4']}, peer={values['WGCF_PEER_ENDPOINT_HOST']}:"
        f"{values['WGCF_PEER_ENDPOINT_PORT']}, reserved={values['WGCF_RESERVED_JSON']}")
    return values


def peer_address(host, say, resolve=None):
    """IP пира WARP для конфига.

    sing-box разрешает домен пира при старте: без DNS это FATAL, и вместе с
    WARP-ногой падают direct-ноги. Поэтому домен разрешается здесь.
    """
    host = host.removeprefix('[').removesuffix(']')
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        found = (resolve or _resolve_v4)(host)
    except OSError:
        found = []
    if found:
        return found[0]
    say(f'⚠ адрес WARP-пира {host} не разрешился, беру {WARP_PEER_FALLBACK}')
    return WARP_PEER_FALLBACK


def _resolve_v4(host):
    return [item[4][0] for item in socket.getaddrinfo(host, None, socket.AF_INET)]


def _password():
    """32 знака алфавита base64: entry принимает секрет экзита только таким."""
    return base64.b64encode(os.urandom(32)).decode().rstrip('=')[:32]


def _key_tail(pem, public, run):
    """32 байта ключа X25519 - хвост DER; запись как в конфиге Reality."""
    argv = ['openssl', 'pkey', '-in', str(pem), '-outform', 'DER'] + (['-pubout'] if public else [])
    der = run(argv, check=True, stdout=subprocess.PIPE).stdout
    return base64.urlsafe_b64encode(der[-32:]).decode().rstrip('=')


def ensure_secrets(etc, env, run=subprocess.run):
    """Пароли каналов, clash-api и пара Reality; существующие не меняются.

    Возвращает (переменные шаблона и Reality, создан ли хоть один файл).
    hy2-mgmt - пароль отдельного служебного инбаунда: ротация клиентских
    его не трогает, и по клиентским кредам метрики не достать.
    """
    etc = Path(etc)
    created = False
    files = {'HY2_PWD_DIRECT': 'hy2-direct.pwd', 'HY2_PWD_WARP': 'hy2-warp.pwd',
             'HY2_OBFS_PWD': 'hy2-obfs.pwd', 'HY2_PWD_MGMT': 'hy2-mgmt.pwd',
             'CLASH_SECRET': 'clash-secret.txt'}
    values = {}
    for variable, name in files.items():
        path = etc / name
        if not path.is_file():
            path.write_text(_password())
            created = True
        path.chmod(0o600)
        values[variable] = path.read_text().rstrip('\n')
    # Запасной канал VLESS + Reality по TCP: entry уходит на него, когда до
    # экзита режут UDP. reality.pub - открытая часть, её забирает деплой entry.
    pem, sid = etc / 'reality.pem', etc / 'reality.sid'
    if not pem.is_file():
        run(['openssl', 'genpkey', '-algorithm', 'X25519', '-out', str(pem)], check=True)
        created = True
    if not sid.is_file():
        sid.write_text(os.urandom(8).hex() + '\n')
        created = True
    pem.chmod(0o600)
    values.update(REALITY_PRIVKEY=_key_tail(pem, False, run),
                  REALITY_SHORT_ID=sid.read_text().rstrip('\n'),
                  REALITY_SNI=env.get('REALITY_SNI') or 'www.apple.com',
                  REALITY_PORT=env.get('REALITY_PORT') or '4443')
    public = (f"PUB={_key_tail(pem, True, run)}\nSID={values['REALITY_SHORT_ID']}\n"
              f"SNI={values['REALITY_SNI']}\nPORT={values['REALITY_PORT']}\n")
    target = etc / 'reality.pub'
    if not target.is_file() or target.read_text() != public:
        target.write_text(public)
    return values, created


def cert_ok(crt, key, pub_ip, run=subprocess.run):
    """Сертификат годен: пара с ключом, SAN - адрес узла, до конца срока больше 30 дней."""
    if not (_nonempty(crt) and _nonempty(key)):
        return False

    def openssl(*argv):
        return run(['openssl', *argv], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    if openssl('x509', '-in', str(crt), '-noout', '-checkend', str(30 * 86400)).returncode != 0:
        return False
    san = openssl('x509', '-in', str(crt), '-noout', '-ext', 'subjectAltName')
    if san.returncode != 0 or not re.search(rf'IP Address:{re.escape(pub_ip)}(,|\n|$)', san.stdout.rstrip('\n')):
        return False
    return (openssl('x509', '-in', str(crt), '-noout', '-pubkey').stdout
            == openssl('pkey', '-in', str(key), '-pubout').stdout)


def ensure_cert(box, etc, pub_ip, say, run=subprocess.run):
    """Самоподписанный сертификат hy2 на 10 лет; True - выпущен заново.

    Entry пинит именно этот сертификат: перевыпуск роняет туннели до следующей
    настройки entry. Поэтому он меняется, только когда негоден.
    """
    crt, key = Path(box) / 'tls.crt', Path(box) / 'tls.key'
    issued = not cert_ok(crt, key, pub_ip, run)
    if issued:
        say(f'генерирую self-signed TLS cert для {pub_ip}')
        run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
             '-keyout', str(key), '-out', str(crt), '-days', '3650', '-nodes',
             '-subj', f'/CN={pub_ip}', '-addext', f'subjectAltName=IP:{pub_ip}'],
            check=True, stderr=subprocess.DEVNULL)
    else:
        say(f'TLS cert для {pub_ip} на месте — оставляю (entry его пинит)')
    for path in (key, crt):
        path.chmod(0o600)
    # Публичная часть для entry: с ней роутер принимает только этот сертификат.
    public = Path(etc) / 'hy2-tls.crt'
    if not public.is_file() or public.read_bytes() != crt.read_bytes():
        public.write_bytes(crt.read_bytes())
    public.chmod(0o644)
    return issued


def render_config(template, values):
    """Конфиг sing-box экзита: шаблон с подстановкой `${ИМЯ}` и инбаунд Reality.

    VLESS + Reality идёт по TCP рядом с hy2: пользователь на канал, у каждого
    маршрут его hy2-инбаунда. uuid выводится из пароля канала так же, как на entry.
    """
    config = json.loads(re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}',
                               lambda match: str(values[match[1]]), template))
    sni = values['REALITY_SNI']
    users = [{'name': kind, 'uuid': str(uuid.uuid5(uuid.NAMESPACE_OID,
                                                   'vpn2-reality:' + values[variable]))}
             for kind, variable in (('direct', 'HY2_PWD_DIRECT'), ('warp', 'HY2_PWD_WARP'),
                                    ('mgmt', 'HY2_PWD_MGMT'))]
    config.setdefault('inbounds', []).append({
        'type': 'vless', 'tag': 'reality-in', 'listen': '::',
        'listen_port': int(values['REALITY_PORT']), 'users': users,
        'tls': {'enabled': True, 'server_name': sni, 'reality': {
            'enabled': True, 'handshake': {'server': sni, 'server_port': 443},
            'private_key': values['REALITY_PRIVKEY'], 'short_id': [values['REALITY_SHORT_ID']]}}})
    route = config.setdefault('route', {})
    rules = []
    for rule in route.get('rules', []):
        rules.append(rule)
        inbound = rule.get('inbound', [])
        inbound = [inbound] if isinstance(inbound, str) else inbound
        rules += [{**rule, 'inbound': 'reality-in', 'auth_user': [user['name']]}
                  for user in users if f"hy2-{user['name']}-in" in inbound]
    route['rules'] = rules + [{'inbound': 'reality-in', 'action': 'reject'}]
    return json.dumps(config, indent=2)


def apply_config(box, text, run=subprocess.run):
    """Проверяет кандидат и атомарно ставит его; True - конфиг сменился.

    Неудачная проверка сохраняет конфиг, с которым служба сможет снова
    стартовать после сбоя или перезагрузки.
    """
    target = Path(box) / 'config.json'
    handle, candidate = tempfile.mkstemp(prefix='.config.json.', dir=box)
    try:
        with os.fdopen(handle, 'w') as stream:
            stream.write(text)
        if run([SING_BOX, 'check', '-c', candidate]).returncode != 0:
            raise RuntimeError('конфиг sing-box невалиден — сервис не трогаю')
        if target.is_file() and target.read_text() == text:
            return False
        os.replace(candidate, target)
        return True
    finally:
        if os.path.exists(candidate):
            os.unlink(candidate)


def bandwidth_fragment(env):
    """Поля скорости клиентских hy2-инбаундов для шаблона.

    Пустая пара скоростей сохраняет BBR. Служебный hy2-mgmt настройка не затрагивает.
    """
    fields = hy2_bandwidth.bandwidth_fields(env.get('HY2_UP_MBPS'), env.get('HY2_DOWN_MBPS'))
    return json.dumps(fields)[1:-1] + ',' if fields else ''


def configure(env, template, say, *, etc='/etc/anysda', box='/etc/sing-box', run=subprocess.run):
    """Профиль WARP, секреты, сертификат и конфиг sing-box; True - узел изменён."""
    for variable in ('PUB_IP', 'HY2_DIRECT_PORT', 'HY2_WARP_PORT', 'HY2_MGMT_PORT'):
        if not env.get(variable):
            raise ValueError(f'{variable} не задан')
    for directory in (etc, box):
        Path(directory).mkdir(parents=True, exist_ok=True)
    values = {name: env[name] for name in ('HY2_DIRECT_PORT', 'HY2_WARP_PORT', 'HY2_MGMT_PORT')}
    values.update(warp_values(etc, say, run))
    secrets, created = ensure_secrets(etc, env, run)
    values.update(secrets)
    values['WGCF_PEER_ENDPOINT_HOST'] = peer_address(values['WGCF_PEER_ENDPOINT_HOST'], say)
    issued = ensure_cert(box, etc, env['PUB_IP'], say, run)
    values['HY2_BANDWIDTH_FIELDS'] = bandwidth_fragment(env)
    changed = apply_config(box, render_config(Path(template).read_text(), values), run)
    return changed or created or issued
