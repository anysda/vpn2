"""Аварийный Hysteria2 одной выдачи: учётки, сертификат, самопроверка, сеансы.

start/stop зовёт агент. Службу ведёт systemd, правила nftables ставят её
ExecStartPre/ExecStopPost через `vpn2 emergency hy2 open|close`.
"""

import argparse
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
from urllib.parse import quote, urlencode

from .. import fsutil
from ..probe import ClashApi

STATE = Path('/var/lib/vpn2-emergency')
OPTIONS = Path('/etc/vpn2/emergency-hy2.json')
TLS_SOURCE = '/etc/vpn2/tls/ip'
UNIT = 'vpn2-emergency-hy2.service'
PROBE_UNIT = 'vpn2-emergency-hy2-probe.service'
MOUNT = Path('/run/vpn2-emergency-hy2')
LOG = logging.getLogger(__name__)
# Clash API самого экземпляра на loopback (номер порта тот же) отдаёт живые
# потоки; у каждого устройства своё правило маршрута, в его имени - учётка.
SESSIONS_STALE = 40
DEVICE_USER = re.compile(r'g(\d+)-d(\d+)')
SESSION_RULE = re.compile(r'auth_user=g(\d+)-d(\d+) ')
INACTIVE = {'generation': -1, 'active': False}
END_CERT = '-----END CERTIFICATE-----'


def command(args, **kwargs):
    """В ошибку не попадают ни вывод, ни аргументы: там могут быть учётки."""
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=30, **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RuntimeError(f'emergency hy2: {Path(args[0]).name} failed') from None


def atomic(path, data):
    fsutil.atomic(path, data if isinstance(data, bytes) else json.dumps(data), 0o640)


def home(state_dir):
    directory = Path(state_dir) / 'hy2'
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    return directory


def read_state(directory):
    try:
        return json.loads((Path(directory) / 'generation.json').read_text())
    except (OSError, ValueError):
        return dict(INACTIVE)


def settings(options=None):
    result = {
        'port': 18443, 'tls_dir': TLS_SOURCE,
        'binary': '/usr/local/bin/sing-box', 'runtime_dir': str(MOUNT),
        'probe_url': 'https://cp.cloudflare.com/generate_204',
    }
    result.update(json.loads(OPTIONS.read_text()) if options is None else options)
    result['server_ip'] = str(ipaddress.ip_address(result['server_ip']))
    if type(result['port']) is not int or not 1024 <= result['port'] <= 65535:
        raise ValueError('emergency hy2: invalid port')
    return result


def expiry(cert, key, server_ip, cafile=None):
    """Конец срока пары, если TLS-клиент с корнями cafile (без него - системными) принимает её для IP."""
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=cafile)
    if cafile:
        client.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    to_client, to_server = ssl.MemoryBIO(), ssl.MemoryBIO()
    sides = (client.wrap_bio(to_client, to_server, server_hostname=server_ip),
             server.wrap_bio(to_server, to_client, server_side=True))
    for _ in range(3):
        for side in sides:
            with contextlib.suppress(ssl.SSLWantReadError):
                side.do_handshake()
    return ssl.cert_time_to_seconds(sides[0].getpeercert()['notAfter'])


def trusted_problem(cert, key, server_ip):
    """None для пары, которой браузер доверяет на IP узла ещё час, иначе причина без секретов."""
    try:
        if expiry(cert, key, server_ip) < time.time() + 3600:
            return 'certificate expires within an hour'
    except ssl.SSLCertVerificationError as error:
        return f'not trusted for {server_ip}: {error.verify_message}'
    except (OSError, ValueError):
        return f'no usable certificate pair at {Path(cert).parent}'
    return None


def read_pair(source):
    try:
        return (source / 'fullchain.pem').read_bytes(), (source / 'privkey.pem').read_bytes()
    except OSError:
        return None


def adopt(directory, server_ip, pair):
    """Поставить общую пару; проверяется та же копия, что встанет на место."""
    incoming = directory / 'incoming-fullchain.pem', directory / 'incoming-privkey.pem'
    atomic(incoming[1], pair[1])
    atomic(incoming[0], pair[0])
    problem = trusted_problem(*incoming, server_ip)
    if not problem:
        # sing-box следит за обоими путями и перечитывает пару после второго переименования.
        os.replace(incoming[1], directory / 'privkey.pem')
        os.replace(incoming[0], directory / 'fullchain.pem')
    for path in incoming:
        path.unlink(missing_ok=True)
    return problem


def fingerprint(directory, server_ip):
    """SHA-256 листа пары, которой браузер не доверяет; None для доверенной."""
    cert = directory / 'fullchain.pem'
    if not trusted_problem(cert, directory / 'privkey.pem', server_ip):
        return None
    leaf = cert.read_text().split(END_CERT)[0] + END_CERT
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(leaf)).hexdigest()


def certificate(directory, server_ip, source=TLS_SOURCE):
    """Пара Hysteria2 и панели: общая на IP узла, иначе прежняя рабочая или новая самоподписанная."""
    pair = read_pair(Path(source))
    problem = adopt(directory, server_ip, pair) if pair else f'no certificate at {source}'
    cert, key = directory / 'fullchain.pem', directory / 'privkey.pem'
    try:
        usable = expiry(cert, key, server_ip, cafile=cert) > time.time() + 3600
    except (OSError, ValueError):
        usable = False
    if problem and not usable:
        LOG.warning('Emergency TLS: %s; self-signed certificate, pinSHA256 in profiles', problem)
        command(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                 '-nodes', '-days', '3650', '-subj', '/CN=emergency',
                 '-addext', f'subjectAltName=IP:{server_ip}',
                 '-keyout', str(key), '-out', str(cert)])
        key.chmod(0o640)
        cert.chmod(0o640)
    return fingerprint(directory, server_ip)


def tls_pair(state_dir, server_ip, source=TLS_SOURCE):
    """Каталог пары для панели; при идущей выдаче пару не трогаем: её pin в профилях."""
    directory = home(state_dir)
    if not read_state(directory)['active']:
        certificate(directory, str(ipaddress.ip_address(server_ip)), source)
    return directory


def refresh_tls(directory, server_ip, source=TLS_SOURCE):
    """Продлённая общая пара встаёт вместо доверенной; самоподписанная остаётся до конца выдачи."""
    pair = read_pair(Path(source))
    if pair is None or pair == read_pair(directory) or fingerprint(directory, server_ip):
        return False
    if adopt(directory, server_ip, pair):
        return False
    LOG.info('Emergency TLS certificate renewed')
    return True


def profile(options, password, name, pin):
    ip = ipaddress.ip_address(options['server_ip'])
    host = f'[{ip}]' if ip.version == 6 else str(ip)
    query = {'sni': str(ip)}
    if pin:
        # Hysteria checks pinSHA256 independently of CA verification. An isolated
        # insecure=1 is never emitted; the fallback must verify the exact leaf.
        query.update(insecure='1', pinSHA256=pin)
    return f"hysteria2://{quote(password, safe='')}@{host}:{options['port']}/?{urlencode(query)}#{quote(name, safe='')}"


def server_config(options, users, api_secret):
    directory = Path(options['runtime_dir'])
    # Правила auth_user маршрут не меняют: по ним Clash API отличает устройства.
    devices = [{'auth_user': [user['name']], 'action': 'route', 'outbound': 'internet'}
               for user in users if DEVICE_USER.fullmatch(user['name'])]
    return {
        'log': {'level': 'warn'},
        # Локальный резолвер узла изолированному пользователю недоступен.
        'dns': {'servers': [{'type': 'tls', 'tag': 'public', 'server': '1.1.1.1'}]},
        'inbounds': [{'type': 'hysteria2', 'tag': 'emergency', 'listen': '::',
                      'listen_port': options['port'], 'users': users,
                      'tls': {'enabled': True, 'certificate_path': str(directory / 'fullchain.pem'),
                              'key_path': str(directory / 'privkey.pem')}}],
        'outbounds': [{'type': 'direct', 'tag': 'internet'}],
        'route': {'default_domain_resolver': 'public', 'rules': [
            {'ip_is_private': True, 'action': 'reject'},
            {'ip_cidr': [options['server_ip']], 'action': 'reject'},
            *devices,
        ]},
        'experimental': {'clash_api': {'external_controller': f"127.0.0.1:{options['port']}",
                                       'secret': api_secret}},
    }


class SystemRuntime:
    def service(self, action):
        # Агент работает с NoNewPrivileges: правило polkit роли разрешает его
        # пользователю start/stop/restart только этих служб.
        command(['/usr/bin/systemctl', action, UNIT])

    def probe(self, _state_dir):
        # sing-box нужен AF_NETLINK, которого в песочнице агента нет: пробу
        # выполняет oneshot-служба, её код возврата и есть ответ.
        try:
            command(['/usr/bin/systemctl', 'start', PROBE_UNIT])
        except RuntimeError:
            return False
        return True


def materials(directory, state):
    result = {}
    for device in state['devices']:
        uri = device['uri']
        # 4 px на модуль - столько панель и показывает на ПК: меньше камера с экрана не берёт.
        png = command(['qrencode', '-t', 'PNG', '-o', '-', '-l', 'L', '-s', '4'], input=uri.encode()).stdout
        path = directory / f"device-{device['id']}.txt"
        atomic(path, (uri + '\n').encode())
        result[device['id']] = {'uri': uri, 'qr_png': png, 'file': str(path)}
    return result


def start(state_dir, generation, devices, *, options=None, runtime=None):
    """Поднять выдачу; повтор той же выдачи (рестарт агента) учётки не меняет."""
    options = settings(options)
    runtime = runtime or SystemRuntime()
    directory = home(state_dir)
    state = read_state(directory)
    resumed = state['active'] and state['generation'] == generation
    if not resumed:
        devices = sorted(({'id': d['id'], 'name': d['name']} for d in devices), key=lambda d: d['id'])
        if type(generation) is not int or generation < 0 or len({d['id'] for d in devices}) != len(devices):
            raise ValueError('emergency hy2: invalid issuance')
        stop(state_dir, runtime=runtime)
        pin = certificate(directory, options['server_ip'], options['tls_dir'])
        users, issued = [], []
        for device in devices:
            password = secrets.token_urlsafe(32)
            users.append({'name': f"g{generation}-d{device['id']}", 'password': password})
            issued.append({**device, 'uri': profile(options, password, device['name'], pin)})
        # У самопроверки своя учётка, пользователю она не выдаётся.
        probe_password, api_secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        users.append({'name': f'g{generation}-readiness', 'password': probe_password})
        atomic(directory / 'server.json', server_config(options, users, api_secret))
        state = {'generation': generation, 'active': True, 'devices': issued, 'options': options,
                 'probe_password': probe_password, 'pin': pin, 'api_secret': api_secret}
        atomic(directory / 'generation.json', state)
    try:
        runtime.service('start' if resumed else 'restart')
        if not runtime.probe(state_dir):
            raise RuntimeError('emergency hy2: real connection self-test failed')
        return materials(directory, state)
    except BaseException:
        stop(state_dir, runtime=runtime)
        raise


def stop(state_dir, *, runtime=None):
    directory = home(state_dir)
    atomic(directory / 'generation.json', {'generation': read_state(directory)['generation'], 'active': False})
    (runtime or SystemRuntime()).service('stop')
    for path in [*directory.glob('device-*.txt'), directory / 'server.json', directory / 'sessions.json']:
        path.unlink(missing_ok=True)


def active_state(directory):
    state = read_state(directory)
    if not state['active']:
        raise ValueError('emergency hy2: inactive generation')
    return state


def listening(port):
    """UDP socket bound to port in this network namespace (IPv4 or IPv6)."""
    suffix = f':{port:04X}'
    for table in ('/proc/net/udp', '/proc/net/udp6'):
        with contextlib.suppress(OSError):
            for line in Path(table).read_text().splitlines()[1:]:
                if line.split()[1].endswith(suffix):
                    return True
    return False


def probe(state_dir):
    """Authenticate via QUIC and fetch a public URL through the actual instance."""
    directory = Path(state_dir) / 'hy2'
    try:
        state = active_state(directory)
        options = state['options']
        deadline = time.monotonic() + 15
        # A QUIC handshake sent before the server binds backs off for seconds.
        while not listening(options['port']):
            if time.monotonic() > deadline:
                return False
            time.sleep(.05)
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            proxy_port = reserved.getsockname()[1]
        config = {
            'log': {'disabled': True},
            'inbounds': [{'type': 'socks', 'listen': '127.0.0.1', 'listen_port': proxy_port}],
            'outbounds': [{'type': 'hysteria2', 'server': '127.0.0.1',
                           'server_port': options['port'], 'password': state['probe_password'],
                           'tls': {'enabled': True, 'server_name': options['server_ip'],
                                   'certificate_path': str(directory / 'fullchain.pem')}}],
        }
        with tempfile.TemporaryDirectory(dir=directory, prefix='probe-') as temporary:
            path = Path(temporary) / 'client.json'
            atomic(path, config)
            process = subprocess.Popen([options['binary'], 'run', '-c', str(path)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                while process.poll() is None and time.monotonic() < deadline:
                    response = subprocess.run([
                        'curl', '--silent', '--fail', '--noproxy', '', '--max-time', '3',
                        '--socks5-hostname', f'127.0.0.1:{proxy_port}', options['probe_url'],
                    ], capture_output=True, timeout=4)
                    if response.returncode == 0:
                        return True
                    time.sleep(.1)
            finally:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return False
    return False


def firewall_rules(uid, port, active):
    """Filter socket owner after DNS resolution, including all node-local IPs.

    Only QUIC replies may bypass destination filters. Existing outbound client
    streams never bypass them, even if conntrack already considers them valid.
    """
    if type(uid) is not int or uid < 1 or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('emergency hy2: invalid firewall identity')
    gate = '' if active else f'udp dport {port} reject'
    return f'''table inet vpn2_emergency_hy2 {{
 chain input {{ type filter hook input priority -10; policy accept;
  {gate}
 }}
 chain output {{ type filter hook output priority -10; policy accept;
  meta skuid {uid} ct direction reply udp sport {port} accept
  meta skuid {uid} ct direction reply tcp sport {port} ip daddr 127.0.0.1 accept
  meta skuid {uid} fib daddr type local reject
  meta skuid {uid} ip daddr {{ 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 }} reject
  meta skuid {uid} ip6 daddr {{ ::/96, ::ffff:0:0/96, 64:ff9b::/96, 64:ff9b:1::/48, 100::/64, 2001::/32, 2002::/16, fc00::/7, fe80::/10, ff00::/8 }} reject
 }}
}}
'''


def live_devices(state, *, timeout=3):
    """Устройства выдачи с живым потоком прямо сейчас."""
    api = ClashApi(f"http://127.0.0.1:{state['options']['port']}", state['api_secret'])
    return {int(match[2]) for connection in api.connections(timeout=timeout)
            if (match := SESSION_RULE.match(connection.get('rule', '')))
            and int(match[1]) == state['generation']}


def poll_sessions(state_dir, *, fetch=live_devices, clock=time.time):
    """Одно наблюдение текущей выдачи в hy2/sessions.json: кто на связи и кто был."""
    directory = Path(state_dir) / 'hy2'
    state = read_state(directory)
    if not state['active']:
        return None
    path = directory / 'sessions.json'
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    issued = {device['id'] for device in state['devices']}
    seen = set(previous.get('seen', [])) if previous.get('generation') == state['generation'] else set()
    try:
        live, ok = fetch(state) & issued, True
    except (OSError, ValueError):
        live, ok = set(), False
    record = {'generation': state['generation'], 'checked_at': clock(), 'ok': ok,
              'live': sorted(live), 'seen': sorted((seen | live) & issued)}
    atomic(path, record)
    return record


def connections(state_dir, generation, *, clock=time.time):
    """{device_id: connected|seen|never} выдачи; {} - состояние неизвестно."""
    directory = Path(state_dir) / 'hy2'
    try:
        state = read_state(directory)
        record = json.loads((directory / 'sessions.json').read_text())
    except (OSError, ValueError):
        return {}
    if (not state['active'] or state['generation'] != generation
            or record.get('generation') != generation or record.get('ok') is not True
            or not abs(clock() - record.get('checked_at', 0)) <= SESSIONS_STALE):
        return {}
    live, seen = set(record['live']), set(record['seen'])
    return {device['id']: 'connected' if device['id'] in live else
            'seen' if device['id'] in seen else 'never' for device in state['devices']}


def firewall(active):
    options = settings()
    uid = pwd.getpwnam('vpn2-hy2').pw_uid
    # add + delete in one transaction: the table is replaced whether it existed or not.
    reset = 'add table inet vpn2_emergency_hy2\ndelete table inet vpn2_emergency_hy2\n'
    command(['nft', '-f', '-'], input=(reset + firewall_rules(uid, options['port'], active)).encode())


def endpoint(server_ip, port, active, path=None):
    """Адрес и порт аварийного Hysteria2; при идущей аварии не меняются."""
    ip = str(ipaddress.ip_address(server_ip))
    port = int(port)
    if not 1024 <= port <= 65535:
        raise SystemExit('invalid emergency UDP port')
    path = Path(path or OPTIONS)
    desired = {'server_ip': ip, 'port': port}
    if path.exists() and json.loads(path.read_text()) != desired and active:
        # Issued profiles name the current endpoint; change it with the next generation.
        raise SystemExit('stop emergency role before changing its endpoint settings')
    path.write_text(json.dumps(desired) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('open', 'close', 'probe', 'endpoint', 'tls-pair'))
    parser.add_argument('values', nargs='*', help='endpoint: IP, порт, 1 при идущей аварии; tls-pair: IP')
    args = parser.parse_args(argv)
    if args.action == 'endpoint':
        server_ip, port, active = args.values
        endpoint(server_ip, port, active == '1')
    elif args.action == 'tls-pair':
        tls_pair(STATE, *args.values)
    elif args.action == 'probe':
        raise SystemExit(0 if probe(STATE) else 1)
    else:
        if args.action == 'open':
            active_state(STATE / 'hy2')
        firewall(args.action == 'open')


if __name__ == '__main__':
    main()
