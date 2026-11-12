#!/usr/bin/env python3
"""Проверка настоящего трафика entry временным клиентом в отдельном netns.

Клиент живёт в netns vpn2-verify и соединён с хостом парой veth: наружу у него
есть только туннель. Реквизиты временные и вне базы панели:
- WireGuard: пир `wg set`;
- OpenVPN: сертификат на сутки от CA без записи в index, ключ только в /run;
- IKEv2: conn и EAP-секрет в своём файле conf.d/vpn2-verify.conf.

По каждому протоколу: подключение, прямой путь, путь через каждый экзит с ручным
правилом, DNS, отрицательный контроль (туннель опущен - пробы обязаны упасть) и
отзыв (старый доступ не работает, новый работает). Путь - HTTP-запрос к внешнему
адресу с доменом-меткой в ручном наборе маршрута панели; выход, через который он
ушёл, берётся из API sing-box (/connections). Внешние адреса недоступны и с самого
entry - «Не проверено», не «Ошибка».

Код: 0 - всё работает, 1 - есть «Ошибка», 2 - есть «Не проверено».
"""
import argparse
import contextlib
import datetime
import fcntl
import http.client
import ipaddress
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

NS = 'vpn2-verify'
HOST_IF = 'vp2verify'
NS_IF = 'vp2verify-c'
HOST_IP = '169.254.253.1'
NS_IP = '169.254.253.2'
MARKER = '.vpn2-verify.invalid'
WORK = Path('/run/anysda-verify')
SWAN_FILE = Path('/etc/swanctl/conf.d/vpn2-verify.conf')
CCD = Path('/etc/openvpn/server/ccd')
OVPN_PKI = Path('/etc/openvpn/server/pki')
MANUAL_DIR = Path('/etc/sing-box/manual-routes')
ROUTER = Path('/etc/sing-box/config.json')
DIRECT = 'direct-ru'
CONTROL_STATE = Path('/var/lib/vpn2-control')
WG_CONF = Path('/etc/wireguard/wg0.conf')
# Панель при каждом выпуске и удалении устройства применяет свой полный список клиентов
# и этим снимает временный доступ проверки: столько раз проверка возвращает его и повторяет.
RESTORES = 8
MOVING = 'панель меняла список клиентов при каждой попытке, причина неразличима: '
INTERNET = (('connectivitycheck.gstatic.com', '/generate_204', 204),
            ('www.google.com', '/generate_204', 204),
            ('checkip.amazonaws.com', '/', 200))
NO_INTERNET = 'внешние адреса недоступны и с самого entry - причина неразличима'
STATUSES = ('Работает', 'Ошибка', 'Не проверено')
OK, FAIL, UNKNOWN = range(3)
MARKS = ('✓', '✗', '?')


def run(*args, data=None, check=True, timeout=30):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} {args[1] if len(args) > 1 else ""}: код {result.returncode}')
    return result


def ns(*args, **kwargs):
    return run('ip', 'netns', 'exec', NS, *args, **kwargs)


def applied():
    """Отметка применений службы управления: её файлы состояния меняются каждым применением."""
    try:
        return sorted((item.name, item.stat().st_mtime_ns, item.stat().st_ino)
                      for item in CONTROL_STATE.iterdir() if item.is_file())
    except OSError:
        return []


class Report:
    """Строки отчёта: проверка, компонент, результат, причина, время."""

    def __init__(self, tag):
        self.tag = tag
        self.rows = []
        self.blurred = False

    def add(self, check, component, status, reason):
        # Попытки исчерпаны под применениями панели: сбой не доказан, но и успеха нет.
        if status == FAIL and self.blurred:
            status, reason = UNKNOWN, MOVING + reason
        self.blurred = False
        self.rows.append({'check': check, 'component': component, 'result': STATUSES[status], 'reason': reason,
                          'time': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
        label = f'{component}: {check}'
        if status == UNKNOWN:
            print(f'[{self.tag}] ? {label} — не проверено: {reason}', flush=True)
        else:
            print(f'[{self.tag}] {MARKS[status]} {label} — {reason}', flush=True)

    def code(self):
        results = {row['result'] for row in self.rows}
        if STATUSES[FAIL] in results:
            return 1
        return 2 if STATUSES[UNKNOWN] in results else 0


def probe_http(ip, host, path='/', timeout=8):
    """Код HTTP-ответа или None. Host - метка ручного маршрута, поэтому чужой сервер отвечает
    404 или 403, но ответить он может, только если выход открыл соединение наружу."""
    connection = http.client.HTTPConnection(ip, 80, timeout=timeout)
    try:
        connection.request('GET', path, headers={'Host': host, 'User-Agent': 'vpn2-verify'})
        response = connection.getresponse()
        response.read(65536)
        # Соединение держим, пока проверка смотрит в API sing-box.
        time.sleep(0.8)
        return response.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def probe_dns(server, name, port=53):
    """Сырой DNS-запрос A: отсортированные адреса ответа ([] без адресов), None - ответа нет."""
    query_id = os.urandom(2)
    question = b''.join(bytes([len(p)]) + p.encode() for p in name.split('.')) + b'\0\0\1\0\1'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(4)
        try:
            sock.connect((server, port))
            sock.send(query_id + b'\1\0\0\1\0\0\0\0\0\0' + question)
            reply = sock.recv(4096)
        except OSError:
            return None
        # UDP-сессию держим: её видно в API sing-box.
        time.sleep(0.5)
    if len(reply) < 12 or reply[:2] != query_id:
        return None
    flags, _questions, answers = struct.unpack('!HHH', reply[2:8])
    if flags & 0x800f != 0x8000 or reply[12:12 + len(question)].lower() != question.lower():
        return None
    offset, found = 12 + len(question), set()
    try:
        for _ in range(answers):
            while reply[offset]:
                if reply[offset] & 0xc0 == 0xc0:
                    offset += 1
                    break
                offset += reply[offset] + 1
            offset += 1
            kind, cls, _ttl, length = struct.unpack('!HHIH', reply[offset:offset + 10])
            offset += 10
            if kind == 1 and cls == 1 and length == 4:
                found.add(socket.inet_ntoa(reply[offset:offset + 4]))
            offset += length
    except (IndexError, struct.error):
        return None
    return sorted(found)


PROBES = {'http': probe_http, 'dns': probe_dns}


def connections():
    """Соединения из API sing-box; API нет - пустой список."""
    try:
        api = json.loads(ROUTER.read_text()).get('experimental', {}).get('clash_api', {})
        request = urllib.request.Request(f'http://{api["external_controller"]}/connections',
                                         headers={'Authorization': 'Bearer ' + api.get('secret', '')})
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response).get('connections') or []
    except (OSError, ValueError, KeyError):
        return []


def resolve(name):
    try:
        return socket.getaddrinfo(name, None, socket.AF_INET)[0][4][0]
    except (OSError, IndexError):
        return None


def in_namespace(source, kind, args, dest=None, deadline=12):
    """Проба в netns и соединения клиента из API sing-box: [(выход, правило)].

    Соединения, открытые до пробы, в счёт не идут: закрытое соединение прошлой
    пробы к тому же адресу ещё висит в /connections и подменило бы выход.
    """
    before = {item.get('id') for item in connections()} if source else set()
    process = subprocess.Popen(['ip', 'netns', 'exec', NS, sys.executable, '-I', str(Path(__file__).absolute()),
                                'probe', kind, json.dumps(args)],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    seen = {}
    stop = time.monotonic() + deadline

    def collect():
        for item in connections() if source else []:
            meta = item.get('metadata', {})
            if item.get('id') not in before and meta.get('sourceIP') == source and (dest is None or (
                    dest[0] in (None, meta.get('destinationIP')) and str(meta.get('destinationPort')) == str(dest[1]))):
                chains = item.get('chains') or ['']
                seen[item.get('id')] = (chains[0], item.get('rule', '') + ' ' + item.get('rulePayload', ''))

    while process.poll() is None and time.monotonic() < stop:
        collect()
        time.sleep(0.05)
    if process.poll() is None:
        process.kill()
    out = process.communicate()[0]
    collect()
    try:
        return json.loads(out), list(seen.values())
    except ValueError:
        return None, list(seen.values())


def namespace_up():
    run('ip', 'netns', 'add', NS)
    run('ip', 'link', 'add', HOST_IF, 'type', 'veth', 'peer', 'name', NS_IF)
    run('ip', 'link', 'set', NS_IF, 'netns', NS)
    run('ip', 'address', 'add', HOST_IP + '/30', 'dev', HOST_IF)
    run('ip', 'link', 'set', HOST_IF, 'up')
    ns('ip', 'link', 'set', 'lo', 'up')
    ns('ip', 'address', 'add', NS_IP + '/30', 'dev', NS_IF)
    ns('ip', 'link', 'set', NS_IF, 'up')
    # Из netns наружу только туннель и транспорт до entry: утечка мимо туннеля невозможна.
    for rule in (['-o', 'lo'], ['-o', 'vpn'], ['-d', HOST_IP], ['-m', 'policy', '--dir', 'out', '--pol', 'ipsec']):
        ns('iptables', '-w', '-A', 'OUTPUT', *rule, '-j', 'ACCEPT')
    ns('iptables', '-w', '-P', 'OUTPUT', 'DROP')


def source():
    """Адрес клиента внутри туннеля: им запрос подписан в API sing-box."""
    route = ns('ip', '-j', 'route', 'get', '192.0.2.1', check=False)
    try:
        src = json.loads(route.stdout)[0].get('prefsrc')
    except (ValueError, IndexError):
        return None
    return src if src and src != NS_IP else None


class WireGuard:
    name = 'WireGuard'
    proto = 'udp'

    def __init__(self):
        self.port = int(run('wg', 'show', 'wg0', 'listen-port').stdout)
        self.server = run('wg', 'show', 'wg0', 'public-key').stdout.strip()
        self.peers = {}
        iface = json.loads(run('ip', '-j', '-4', 'address', 'show', 'dev', 'wg0').stdout)[0]['addr_info'][0]
        network = ipaddress.ip_interface(f"{iface['local']}/{iface['prefixlen']}").network
        used = set(re.findall(r'(\d+\.\d+\.\d+\.\d+)/32', run('wg', 'show', 'wg0', 'allowed-ips').stdout))
        with contextlib.suppress(OSError):
            used |= set(re.findall(r'(\d+\.\d+\.\d+\.\d+)/32', WG_CONF.read_bytes().decode('latin-1')))
        self.free = [str(a) for a in reversed(list(network.hosts())) if str(a) not in used | {iface['local']}][:2]

    def issue(self, identity):
        private = run('wg', 'genkey').stdout.strip()
        public = run('wg', 'pubkey', data=private + '\n').stdout.strip()
        # Закрытый ключ только в памяти: на диске лишь открытый, по нему уборка снимает пира.
        (WORK / f'wg-{identity}.pub').write_text(public + '\n')
        self.peers[identity] = {'public': public, 'address': self.free.pop(0), 'key': private, 'revoked': False}

    def ensure(self):
        """Панель раз в минуту делает syncconf и снимает чужих пиров: возвращаем своих."""
        for peer in self.peers.values():
            if not peer['revoked']:
                run('wg', 'set', 'wg0', 'peer', peer['public'], 'allowed-ips', peer['address'] + '/32')
        return bool(self.peers)

    def connect(self, identity):
        peer = self.peers[identity]
        self.down()
        ns('ip', 'link', 'add', 'vpn', 'type', 'wireguard')
        # Ключ через stdin: профиль AppArmor для wg не даёт читать файлы вне /etc/wireguard.
        ns('wg', 'set', 'vpn', 'private-key', '/dev/stdin', 'peer', self.server, 'endpoint', f'{HOST_IP}:{self.port}',
           'allowed-ips', '0.0.0.0/0', 'persistent-keepalive', '1', data=peer['key'] + '\n')
        ns('ip', 'address', 'add', peer['address'] + '/32', 'dev', 'vpn')
        ns('ip', 'link', 'set', 'vpn', 'up')
        ns('ip', 'route', 'replace', 'default', 'dev', 'vpn')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            shake = ns('wg', 'show', 'vpn', 'latest-handshakes', check=False).stdout.split()
            if len(shake) == 2 and shake[1] != '0':
                return True
            time.sleep(0.3)
        return False

    def down(self):
        ns('ip', 'link', 'del', 'vpn', check=False)

    def revoke(self, identity):
        peer = self.peers[identity]
        peer['revoked'] = True
        run('wg', 'set', 'wg0', 'peer', peer['public'], 'remove')

    def lost(self, identity, _linked):
        """Пира проверки на сервере нет, хотя он не отозван: его снял чужой syncconf."""
        peer = self.peers[identity]
        return not peer['revoked'] and peer['public'] not in run('wg', 'show', 'wg0', 'peers').stdout.split()

    def cleanup(self):
        self.down()


class OpenVPN:
    name = 'OpenVPN'

    def __init__(self):
        conf = Path('/etc/openvpn/server/server.conf').read_text()
        self.port = int(re.search(r'^port\s+(\d+)', conf, re.M)[1])
        self.proto = 'tcp' if re.search(r'^proto\s+tcp', conf, re.M) else 'udp'
        self.process = None
        self.names = {}

    def issue(self, identity):
        name = f'vpn2-verify-{secrets.token_hex(4)}-{identity}'
        key, csr, crt, ext = (WORK / f'ovpn-{identity}{s}' for s in ('.key', '.csr', '.crt', '.ext'))
        run('openssl', 'req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
            '-subj', '/CN=' + name, '-keyout', str(key), '-out', str(csr))
        ext.write_text('basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=clientAuth\n')
        # Без `openssl ca`: index, serial и CRL панели не меняются.
        run('openssl', 'x509', '-req', '-in', str(csr), '-CA', str(OVPN_PKI / 'ca.crt'),
            '-CAkey', str(OVPN_PKI / 'ca.key'), '-set_serial', '0x' + secrets.token_hex(16), '-days', '1',
            '-extfile', str(ext), '-out', str(crt))
        self.names[identity] = name
        inline = ''.join(f'<{tag}>\n{path.read_text().strip()}\n</{tag}>\n' for tag, path in (
            ('ca', OVPN_PKI / 'ca.crt'), ('cert', crt), ('key', key), ('tls-crypt', OVPN_PKI / 'tls-crypt.key')))
        (WORK / f'ovpn-{identity}.conf').write_text(
            'client\ndev vpn\ndev-type tun\nnobind\nroute-nopull\nremote-cert-tls server\n'
            'cipher AES-256-GCM\ndata-ciphers AES-256-GCM:CHACHA20-POLY1305\nauth SHA256\n'
            'connect-retry 1\nconnect-retry-max 3\nserver-poll-timeout 5\nverb 1\n' + inline)

    def connect(self, identity):
        self.down()
        self.process = subprocess.Popen(
            ['ip', 'netns', 'exec', NS, 'openvpn', '--config', str(WORK / f'ovpn-{identity}.conf'),
             '--proto', 'udp' if self.proto == 'udp' else 'tcp-client', '--remote', HOST_IP, str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self.process.poll() is None:
            info = ns('ip', '-j', '-4', 'address', 'show', 'dev', 'vpn', check=False)
            with contextlib.suppress(ValueError, IndexError):
                if info.returncode == 0 and json.loads(info.stdout)[0].get('addr_info'):
                    ns('ip', 'route', 'replace', 'default', 'dev', 'vpn')
                    return True
            time.sleep(0.3)
        return False

    def down(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None

    def revoke(self, identity):
        name = self.names[identity]
        # Так же панель выключает клиента: disable в ccd и разрыв живой сессии.
        (CCD / name).write_text('disable\n')
        # openvpn читает ccd после сброса прав до nobody, а umask проверки 077.
        (CCD / name).chmod(0o644)
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(5)
            uid = os.geteuid()
            try:
                if Path('/etc/vpn2-control/api-managed').exists():
                    os.seteuid(pwd.getpwnam('vpn2-control').pw_uid)
                sock.connect('/etc/openvpn/server/mgmt.sock')
            finally:
                os.seteuid(uid)
            sock.sendall(f'kill {name}\nquit\n'.encode())
            with contextlib.suppress(OSError):
                sock.recv(4096)

    def ensure(self):
        pass

    def lost(self, _identity, _linked):
        return False

    def cleanup(self):
        self.down()


class IKEv2:
    name = 'IKEv2'
    proto = 'udp'

    def __init__(self):
        self.server_id = Path('/etc/anysda/ikev2-server-host').read_text().strip()
        if not re.fullmatch(r'[A-Za-z0-9.:-]+', self.server_id):
            raise ValueError('идентификатор сервера IKEv2')
        self.port = 500
        self.secrets = {}
        self.process = None
        self.run_dir = WORK / 'charon'

    def server_conf(self):
        live = [(i, s) for i, s in self.secrets.items() if not s['revoked']]
        conns = ''.join(f'  vpn2-verify-{i} : connections.anysda-ikev2 {{\n'
                        f'    remote-client {{\n      eap_id = "{s["id"]}"\n      groups =\n    }}\n  }}\n'
                        for i, s in live)
        creds = ''.join(f'  eap-vpn2-verify-{i} {{\n    id = "{s["id"]}"\n    secret = "{s["secret"]}"\n  }}\n'
                        for i, s in live)
        SWAN_FILE.write_text(f'connections {{\n{conns}}}\nsecrets {{\n{creds}}}\n')
        SWAN_FILE.chmod(0o600)
        run('swanctl', '--load-all', '--noprompt')

    def issue(self, identity):
        self.secrets[identity] = {'id': f'vpn2-verify-{secrets.token_hex(4)}-{identity}',
                                  'secret': secrets.token_hex(24), 'revoked': False}
        self.server_conf()

    def vici(self):
        return 'unix://' + str(self.run_dir / 'charon.vici')

    def start_charon(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.run_dir.mkdir(mode=0o700, exist_ok=True)
        # Свой /run и strongswan.conf в своём mount-namespace (AppArmor держит путь конфига);
        # плагины перечислены явно, чтобы плагины хоста не перехватывали трафик клиента.
        (self.run_dir / 'strongswan.conf').write_text(
            'charon {\n  install_dns = no\n  load_modular = no\n'
            '  load = random nonce aes sha1 sha2 hmac gmp md4 openssl pem pkcs1 pkcs8 x509 pubkey constraints '
            'revocation kernel-netlink socket-default vici eap-identity eap-mschapv2\n}\n')
        self.process = subprocess.Popen(
            ['ip', 'netns', 'exec', NS, 'unshare', '-m', 'sh', '-c',
             'mount --bind "$0" /run && mount --bind /run/strongswan.conf /etc/strongswan.conf '
             '&& exec /usr/lib/ipsec/charon', str(self.run_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (self.run_dir / 'charon.vici').exists():
            if self.process.poll() is not None:
                raise RuntimeError('charon клиента не запустился')
            time.sleep(0.1)

    def connect(self, identity):
        self.down()
        self.start_charon()
        ns('ip', 'route', 'replace', 'default', 'via', HOST_IP)
        # AppArmor пускает swanctl только в /etc/swanctl: клиентский каталог подставляется
        # туда bind-монтированием в своём mount-namespace, серверный не меняется.
        client = WORK / 'swanctl'
        (client / 'x509').mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile('/etc/swanctl/x509/anysda-server.crt', client / 'x509/anysda-server.crt')
        secret = self.secrets[identity]
        conf = client / 'swanctl.conf'
        conf.write_text(
            f'connections {{\n  vpn {{\n    version = 2\n    remote_addrs = {HOST_IP}\n'
            '    vips = 0.0.0.0\n    encap = yes\n    proposals = aes256gcm16-prfsha384-ecp384\n'
            f'    local {{\n      auth = eap-mschapv2\n      id = "{secret["id"]}"\n'
            f'      eap_id = "{secret["id"]}"\n    }}\n'
            f'    remote {{\n      auth = pubkey\n      id = {self.server_id}\n'
            '      certs = anysda-server.crt\n    }\n'
            '    children {\n      vpn {\n        remote_ts = 0.0.0.0/0\n        esp_proposals = aes256gcm16\n'
            '      }\n    }\n  }\n}\n'
            f'secrets {{\n  eap-client {{\n    id = "{secret["id"]}"\n    secret = "{secret["secret"]}"\n  }}\n}}\n')
        conf.chmod(0o600)
        run('unshare', '-m', 'sh', '-c', 'mount --bind "$0" /etc/swanctl && exec swanctl --load-all --noprompt '
            '--uri "$1"', str(client), self.vici())
        result = run('swanctl', '--initiate', '--child', 'vpn', '--uri', self.vici(), '--timeout', '15',
                     check=False, timeout=25)
        return result.returncode == 0 and source() is not None

    def down(self):
        if self.process is not None and self.process.poll() is None:
            run('swanctl', '--terminate', '--ike', 'vpn', '--force', '--timeout', '5', '--uri', self.vici(),
                check=False)

    def ensure(self):
        pass

    @staticmethod
    def server_sas(eap_id):
        """uniqueid живых IKE_SA сервера с этим EAP-логином, разбор как у vpn2.control.backend.

        EAP-логин swanctl печатает, только если он отличается от IKE-идентификатора; у
        проверочного клиента они совпадают, и логин стоит в «remote '...'».
        """
        ids, current = [], None
        for line in run('swanctl', '--list-sas', check=False).stdout.splitlines():
            head = re.match(r'^\S+:\s+#(\d+),', line)
            if head:
                current = head[1]
            identity = re.search(r"EAP:\s+'([^']+)'", line) or re.match(r"^\s+remote\s+'([^']+)'", line)
            if identity and current:
                if identity[1] == eap_id:
                    ids.append(current)
                current = None
        return ids

    def terminate(self, eap_id, wait=20):
        # SA логина может стоять под общей conn anysda-ikev2: снимаем каждую по id, пока не останется.
        deadline = time.monotonic() + wait
        while (ids := self.server_sas(eap_id)) and time.monotonic() < deadline:
            for sa in ids:
                run('swanctl', '--terminate', '--ike-id', sa, '--force', '--timeout', '5', check=False)
            time.sleep(0.5)

    def revoke(self, identity):
        secret = self.secrets[identity]
        secret['revoked'] = True
        self.server_conf()
        self.terminate(secret['id'])

    def lost(self, identity, linked):
        """Подключение удалось, а сессии на сервере нет: её закрыла служба управления списком панели."""
        return bool(linked) and not self.server_sas(self.secrets[identity]['id'])

    def cleanup(self):
        self.down()
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        if SWAN_FILE.exists():
            SWAN_FILE.unlink()
            run('swanctl', '--load-all', '--noprompt', check=False)
        for secret in self.secrets.values():
            self.terminate(secret['id'], wait=10)


def manual_markers(tags, add):
    """Домен-метка в ручном наборе панели (pin-hy2-<экзит>-direct, direct-ru); sing-box перечитывает набор сам.

    Ручные наборы стоят выше гео-правил: метка уводит запрос в выход набора.
    """
    markers = {}
    for path in sorted(MANUAL_DIR.glob('*.json')) if MANUAL_DIR.is_dir() else []:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        rules = [r for r in data.get('rules', []) if not (isinstance(r, dict) and any(
            str(d).endswith(MARKER) for d in r.get('domain_suffix', [])))]
        tag = path.stem.removeprefix('pin-hy2-').removesuffix('-direct') if path.stem.startswith('pin-hy2-') \
            and path.stem.endswith('-direct') else DIRECT if path.stem == DIRECT else None
        if add and tag in tags:
            markers[tag] = f'{secrets.token_hex(6)}.{tag}{MARKER}'
            rules.insert(0, {'domain_suffix': [markers[tag]]})
        if rules != data.get('rules', []):
            data['rules'] = rules
            temporary = path.with_name(path.name + '.vpn2-verify.tmp')
            temporary.write_text(json.dumps(data, indent=2))
            temporary.chmod(0o600)
            temporary.replace(path)
    return markers


def server_peers():
    return run('wg', 'show', 'wg0', 'peers', check=False).stdout.split()


def leftovers():
    found = []
    if NS in run('ip', 'netns', 'list', check=False).stdout.split():
        found.append('netns')
    if run('ip', 'link', 'show', HOST_IF, check=False).returncode == 0:
        found.append('veth')
    if SWAN_FILE.exists():
        found.append('conn IKEv2')
    if any(CCD.glob('vpn2-verify-*')):
        found.append('ccd OpenVPN')
    peers = server_peers()
    if any(path.read_text().strip() in peers for path in WORK.glob('wg-*.pub')):
        found.append('пир WireGuard')
    if any(MARKER in path.read_text() for path in MANUAL_DIR.glob('*.json')):
        found.append('ручное правило')
    return found


def cleanup(protocols=()):
    """Уборка и после прерванного прошлого запуска (SIGKILL, обрыв ssh): по признакам, а не по журналу."""
    for protocol in protocols:
        with contextlib.suppress(OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            protocol.cleanup()
    for pid in run('ip', 'netns', 'pids', NS, check=False).stdout.split():
        with contextlib.suppress(ProcessLookupError, ValueError):
            os.kill(int(pid), signal.SIGKILL)
    # Ссылка опускается до удаления: иначе networkd-dispatcher пишет ошибку в журнал.
    if run('ip', 'link', 'set', HOST_IF, 'down', check=False).returncode == 0:
        time.sleep(1)
    run('ip', 'netns', 'del', NS, check=False)
    run('ip', 'link', 'del', HOST_IF, check=False)
    for path in CCD.glob('vpn2-verify-*'):
        path.unlink(missing_ok=True)
    if SWAN_FILE.exists():
        SWAN_FILE.unlink()
        run('swanctl', '--load-all', '--noprompt', check=False)
    manual_markers((), add=False)
    peers = server_peers()
    for path in WORK.glob('wg-*.pub'):
        if path.read_text().strip() in peers:
            run('wg', 'set', 'wg0', 'peer', path.read_text().strip(), 'remove', check=False)
    shutil.rmtree(WORK, ignore_errors=True)


class Verifier:
    def __init__(self, report, exits):
        self.report = report
        self.exits = exits
        self.markers = manual_markers([*exits, DIRECT], add=True)
        # Эталон с самого entry: недоступный и отсюда адрес не годится в пробу пути.
        self.internet = [ip for host, path, status in INTERNET
                         if (ip := resolve(host)) and probe_http(ip, host, path, timeout=5) == status]
        self.dns_reference = {server: probe_dns(server, 'dns.google') for server in ('10.99.0.1', '127.0.0.2')}
        # Действующий доступ: кто подключён, отметка службы управления до подключения, удалось ли оно.
        self.link = self.stamp = None
        self.linked = False

    def add(self, check, protocol, status, reason):
        self.report.add(check, protocol.name, status, reason)

    def attach(self, protocol):
        self.stamp = applied()
        protocol.ensure()
        self.linked = bool(protocol.connect(self.link))
        return self.linked

    def moved(self, protocol):
        """След панели: служба управления применяла список после подключения или доступ пропал с сервера."""
        return applied() != self.stamp or protocol.lost(self.link, self.linked)

    def connect(self, protocol, identity):
        """Подключение доступа; помешала панель - повтор, пока она не оставит попытку в покое."""
        self.report.blurred = False
        self.link = identity
        for _ in range(RESTORES + 1):
            if self.attach(protocol):
                return True
            if not self.moved(protocol):
                return False
        self.report.blurred = True
        return False

    def probe(self, protocol, kind, args, dest=None, retry=True):
        """Проба; неудача под применением панели - доступ возвращается и проба повторяется.

        Без следа панели неудача пробы остаётся неудачей.
        """
        result, seen = in_namespace(source(), kind, args, dest)
        if result is not None or not retry:
            return result, seen
        self.report.blurred = False
        for _ in range(RESTORES):
            if not self.moved(protocol):
                break
            self.attach(protocol)
            result, seen = in_namespace(source(), kind, args, dest)
            if result is not None:
                return result, seen
        else:
            self.report.blurred = True
            return result, seen
        if protocol.ensure():
            time.sleep(2)
            result, seen = in_namespace(source(), kind, args, dest)
        return result, seen

    def route(self, protocol, marker, expected):
        """Первая ответившая проба с меткой; набор правил sing-box перечитывает не мгновенно."""
        for _ in range(4):
            for ip in self.internet:
                result, seen = self.probe(protocol, 'http', [ip, marker], (ip, 80))
                if result is not None:
                    break
            leaves = {leaf for leaf, _rule in seen}
            if result is not None and leaves == {expected} or self.report.blurred:
                break
            time.sleep(3)
        return result is not None and leaves == {expected}, leaves, {rule for _leaf, rule in seen}

    def paths(self, protocol):
        for tag in [DIRECT, *self.exits]:
            label, expected = ('маршрут: прямой', DIRECT) if tag == DIRECT else \
                (f'маршрут: через экзит {tag}', f'hy2-{tag}-direct')
            if tag not in self.markers:
                self.add(label, protocol, UNKNOWN, f'нет ручного набора {DIRECT}' if tag == DIRECT else
                         'нет набора pin-hy2-*-direct')
                continue
            if not self.internet:
                self.add(label, protocol, UNKNOWN, NO_INTERNET)
                continue
            ok, leaves, rules = self.route(protocol, self.markers[tag], expected)
            blurred = self.report.blurred  # строка ручного правила - о той же пробе
            self.add(label, protocol, OK if ok else FAIL, f'внешний адрес ответил через {expected}' if ok else
                     f'нет ответа или неверный выход: {sorted(leaves) or "нет соединения"}')
            if tag != DIRECT:
                manual = ok and any(f'manual-pin-hy2-{tag}-direct' in rule for rule in rules)
                self.report.blurred = blurred
                self.add(f'ручное правило {tag}', protocol, OK if manual else FAIL,
                         'запрос выбрал ручной маршрут панели' if manual else 'ручной маршрут не сработал')

    def dns(self, protocol):
        for server, label, chain in (('10.99.0.1', 'DNS AdGuard', None), ('8.8.8.8', 'DNS внешний', 'dns-unfiltered')):
            reference = self.dns_reference['10.99.0.1' if chain is None else '127.0.0.2']
            # sing-box перехватывает DNS и показывает назначением свой адрес: сверяем только порт.
            result, seen = self.probe(protocol, 'dns', [server, 'dns.google'], (None, 53) if chain else None)
            leaves = {leaf for leaf, _rule in seen} if chain else set()
            if not reference:
                self.add(label, protocol, UNKNOWN, 'нет эталонного ответа резолвера с самого entry')
            elif result and set(result) & set(reference) and (chain is None or leaves == {chain}):
                self.add(label, protocol, OK, f'ответ совпал с эталоном{" через " + chain if chain else ""}')
            else:
                self.add(label, protocol, FAIL,
                         f'нет ответа или чужой путь: {sorted(leaves) if chain else "нет ответа"}')

    def alive(self, protocol, retry=True):
        """Туннель передаёт данные: DNS entry отвечает через него."""
        return self.probe(protocol, 'dns', ['10.99.0.1', 'dns.google'], retry=retry)[0] is not None

    def negative(self, protocol):
        protocol.down()
        leaks = []
        if self.internet and self.probe(protocol, 'http', [self.internet[0], DIRECT + MARKER], retry=False)[0]:
            leaks.append('http')
        if self.alive(protocol, retry=False):
            leaks.append('dns')
        self.add('отрицательный контроль', protocol, FAIL if leaks else OK,
                 f'при опущенном туннеле прошли: {", ".join(leaks)}' if leaks else
                 'туннель опущен - запросы не проходят')

    def revocation(self, protocol):
        if not self.connect(protocol, 'a') or not self.alive(protocol):
            self.add('отзыв', protocol, FAIL, 'повторное подключение старого доступа до отзыва не удалось')
            return
        protocol.revoke('a')
        time.sleep(1)
        live = self.alive(protocol, retry=False)
        again = protocol.connect('a') and self.alive(protocol, retry=False)
        protocol.issue('b')
        fresh = self.connect(protocol, 'b') and self.alive(protocol)
        if not live and not again and fresh:
            self.add('отзыв', protocol, OK, 'старый доступ и его сессия закрыты, новый передаёт данные')
        else:
            self.add('отзыв', protocol, FAIL, 'после отзыва: ' + ', '.join(
                text for bad, text in ((live, 'живая сессия работает'), (again, 'старый доступ подключается'),
                                       (not fresh, 'действующий доступ не работает')) if bad))

    def protocol(self, protocol):
        protocol.issue('a')
        if not self.connect(protocol, 'a') or not source():
            self.add('подключение', protocol, FAIL, f'клиент не подключился к {protocol.proto}/{protocol.port}')
            self.add('маршруты, DNS, отзыв', protocol, UNKNOWN, 'нет подключения')
            return
        self.add('подключение', protocol, OK, 'временный клиент подключён и получил адрес в туннеле')
        self.paths(protocol)
        self.dns(protocol)
        self.negative(protocol)
        self.revocation(protocol)


def verify(report, exits):
    protocols = []
    try:
        cleanup()
        WORK.mkdir(mode=0o700, parents=True, exist_ok=True)
        namespace_up()
        protocols = [WireGuard(), OpenVPN(), IKEv2()]
        verifier = Verifier(report, exits)
        for protocol in protocols:
            try:
                verifier.protocol(protocol)
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
                report.add('исполнение', protocol.name, FAIL, f'проверка прервана: {error}')
            finally:
                protocol.cleanup()
    finally:
        cleanup(protocols)
        rest = leftovers()
        report.add('восстановление', 'очистка', FAIL if rest else OK,
                   'остались: ' + ', '.join(rest) if rest else 'временные реквизиты, netns и метки убраны')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    probe = sub.add_parser('probe')
    probe.add_argument('kind', choices=sorted(PROBES))
    probe.add_argument('args')
    check = sub.add_parser('check')
    check.add_argument('--tag', default='ru')
    check.add_argument('--exits', default='')
    check.add_argument('--report', help='дописать строки отчёта (JSON Lines)')
    sub.add_parser('cleanup')
    args = parser.parse_args(argv)
    if args.command == 'probe':
        print(json.dumps(PROBES[args.kind](*json.loads(args.args))))
        return 0
    if args.command == 'cleanup':
        cleanup()
        return 0
    os.umask(0o077)
    report = Report(args.tag)

    def interrupted(_signal, _frame):
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    lock = open('/run/anysda-verify.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        report.add('блокировка', 'контрольный клиент', UNKNOWN, 'другая проверка трафика ещё идёт')
    else:
        verify(report, args.exits.split())
    if args.report:
        with open(args.report, 'a') as rows:
            for row in report.rows:
                rows.write(json.dumps(row, ensure_ascii=False) + '\n')
    return report.code()


if __name__ == '__main__':
    sys.exit(main())
