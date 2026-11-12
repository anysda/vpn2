#!/usr/bin/env python3
"""Проверка настоящего трафика entry временным клиентом в отдельном netns.

Клиент живёт в netns vpn2-verify и соединён с хостом парой veth: из netns
наружу есть только туннель, маршрута хоста у него нет. Реквизиты временные и
вне базы панели, поэтому в списки клиентов, рассылку и статистику не попадают:
- WireGuard: пир `wg set` (панель всё равно снимает чужие пиры при syncconf);
- OpenVPN: сертификат на сутки от CA без записи в index, ключ только в /run;
- IKEv2: conn и EAP-секрет в своём файле conf.d/vpn2-verify.conf.

По каждому протоколу: подключение, прямой путь, путь через каждый экзит с
ручным правилом, выход в интернет через экзит, DNS, отрицательный контроль
(туннель опущен - пробы обязаны упасть), отзыв (старый доступ не работает,
новый работает) и внешняя проба портов: клиент подключается через SOCKS
tg-proxy, то есть пакеты приходят на entry с адреса экзита. Отдельно - путь
панели: тестовый пир WireGuard через wg.apply службы управления появляется в
wg0 и тем же методом убирается.

Привязка к пути - по API sing-box (/connections): запрос от адреса клиента
в туннеле и выход, через который он ушёл. Цели контролируемые: эхо-сервер
vpn2.verify.target на entry и экзитах. Внешний адрес интернета сверяется с
эталоном с самого entry: недоступен и оттуда - «Не проверено», не «Ошибка».

Код: 0 - всё работает, 1 - есть «Ошибка», 2 - есть «Не проверено».
Секретов в выводе нет. Остатки прошлого прерванного запуска убираются в начале.
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
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

# Проба в netns и тесты запускают модуль файлом: пакет лежит двумя каталогами выше.
try:
    from vpn2 import wg as wg_conf
except ImportError:
    sys.path.insert(0, str(Path(__file__).absolute().parents[2]))
    from vpn2 import wg as wg_conf


def conf_peers(data):
    """Пиры wg0.conf, какими их держит сам wg: одним путём с бэкапом (vpn2.wg)."""
    return wg_conf.read(data)[1]


NS = 'vpn2-verify'
HOST_IF = 'vp2verify'
NS_IF = 'vp2verify-c'
HOST_IP = '169.254.253.1'
NS_IP = '169.254.253.2'
COMMENT = 'vpn2-verify'
MARKER = '.vpn2-verify.invalid'
WORK = Path('/run/anysda-verify')
SWAN_FILE = Path('/etc/swanctl/conf.d/vpn2-verify.conf')
CCD = Path('/etc/openvpn/server/ccd')
OVPN_PKI = Path('/etc/openvpn/server/pki')
MANUAL_DIR = Path('/etc/sing-box/manual-routes')
ROUTER = Path('/etc/sing-box/config.json')
# Прямой выход и одноимённый ручной набор панели.
DIRECT = 'direct-ru'
# Эхо прямого пути: адрес RFC 5737 локальным маршрутом через WAN прямого выхода (клиенту открыты
# только tcp 22/80/443 адресов entry); новый адрес на WAN sing-box принял бы за смену сети.
ECHO_IP = '203.0.113.84'
# Эхо пути через экзит - тот же приём на экзите: локальный маршрут на lo.
EXIT_ECHO_IP = '203.0.113.85'
EXIT_ECHO_ROUTE = f'local {EXIT_ECHO_IP}/32 dev lo table local'
TG_PROXY = ('127.0.0.1', 7897)
TARGET = Path(__file__).with_name('target.py')
# Служба управления (vpn2.control): панель применяет пиров WireGuard её методом wg.apply.
CONTROL_SOCKET = Path('/run/vpn2-control/control.sock')
CONTROL_STATE = Path('/var/lib/vpn2-control')
CONTROL_WG_STATE = CONTROL_STATE / 'wg.json'
# Панель при каждом выпуске и удалении устройства применяет свой полный список
# клиентов и этим снимает временный доступ проверки. Столько раз подряд проверка
# возвращает доступ и повторяет попытку; дальше - «не проверено».
RESTORES = 8
MOVING = 'панель меняла список клиентов при каждой попытке, причина неразличима: '
WG_CONF = Path('/etc/wireguard/wg0.conf')
INTERNET = (('connectivitycheck.gstatic.com', '/generate_204', 204),
            ('www.google.com', '/generate_204', 204),
            ('checkip.amazonaws.com', '/', 200))
STATUSES = ('Работает', 'Ошибка', 'Не проверено', 'Не требуется')
OK, FAIL, UNKNOWN, NOT_NEEDED = range(4)
MARKS = ('✓', '✗', '?', '⊘')


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
        row = {'check': check, 'component': component, 'result': STATUSES[status], 'reason': reason,
               'time': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
        self.rows.append(row)
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


def probe_echo(ip, port, host):
    """POST nonce на цель эха (target.py); ответ - тот же nonce и адрес отправителя."""
    nonce = secrets.token_hex(16)
    connection = http.client.HTTPConnection(ip, port, timeout=5)
    try:
        connection.request('POST', '/vpn2-verify/' + nonce, body=nonce.encode(),
                           headers={'Host': host, 'Content-Type': 'text/plain'})
        response = connection.getresponse()
        data = json.loads(response.read(4096))
        if response.status != 200 or data.get('nonce') != nonce:
            return None
        return {'peer': str(data['peer']), 'local': data['local'] is True}
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def probe_egress(ip, host, path='/', timeout=8):
    """Код HTTP-ответа или None. Host - метка ручного маршрута экзита, поэтому чужой сервер
    отвечает 404 или 403, но ответить он может, только если экзит открыл соединение наружу."""
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


def probe_internet(ip, host, path, status):
    return probe_egress(ip, host, path, timeout=5) == status


def probe_dns(server, name, port=53):
    """Сырой DNS-запрос A; ответ - отсортированный список адресов или None."""
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
    return sorted(found) or None


PROBES = {'echo': probe_echo, 'internet': probe_internet, 'egress': probe_egress, 'dns': probe_dns}


class Router:
    def __init__(self):
        config = json.loads(ROUTER.read_text())
        api = config.get('experimental', {}).get('clash_api', {})
        self.controller = api.get('external_controller', '')
        self.secret = api.get('secret', '')
        self.outbounds = {o['tag']: o for o in config.get('outbounds', [])}

    def connections(self):
        if not self.controller:
            return []
        request = urllib.request.Request(f'http://{self.controller}/connections',
                                         headers={'Authorization': 'Bearer ' + self.secret})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.load(response).get('connections') or []
        except (OSError, ValueError):
            return []

    def exit_ip(self, tag):
        for name in (f'transport-hy2-{tag}-direct', f'hy2-{tag}-direct'):
            server = self.outbounds.get(name, {}).get('server')
            if server:
                return resolve(server)
        return None


def resolve(name):
    try:
        return str(ipaddress.IPv4Address(name))
    except ValueError:
        pass
    try:
        return socket.getaddrinfo(name, None, socket.AF_INET)[0][4][0]
    except (OSError, IndexError):
        return None


def in_namespace(router, source, kind, args, dest=None, deadline=12):
    """Запускает пробу в netns и собирает соединения клиента из API sing-box.

    Соединения, открытые до пробы, в счёт не идут: закрытое соединение прошлой
    пробы к тому же адресу ещё висит в /connections и подменило бы выход.
    """
    before = {item.get('id') for item in router.connections()} if source else set()
    process = subprocess.Popen(['ip', 'netns', 'exec', NS, sys.executable, '-I', str(Path(__file__).absolute()),
                                'probe', kind, json.dumps(args)],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    seen = {}
    stop = time.monotonic() + deadline

    def collect():
        for item in router.connections() if source else []:
            meta = item.get('metadata', {})
            if item.get('id') not in before and meta.get('sourceIP') == source and (dest is None or (
                    dest[0] in (None, meta.get('destinationIP')) and str(meta.get('destinationPort')) == str(dest[1]))):
                seen[item.get('id')] = {'chains': item.get('chains') or [], 'rule': item.get('rule', ''),
                                        'payload': item.get('rulePayload', '')}

    while process.poll() is None and time.monotonic() < stop:
        collect()
        time.sleep(0.05)
    if process.poll() is None:
        process.kill()
    out = process.communicate()[0]
    collect()
    try:
        result = json.loads(out)
    except ValueError:
        result = None
    return result, list(seen.values())


class Namespace:
    def up(self):
        run('ip', 'netns', 'add', NS)
        run('ip', 'link', 'add', HOST_IF, 'type', 'veth', 'peer', 'name', NS_IF)
        run('ip', 'link', 'set', NS_IF, 'netns', NS)
        run('ip', 'address', 'add', HOST_IP + '/30', 'dev', HOST_IF)
        run('ip', 'link', 'set', HOST_IF, 'up')
        ns('ip', 'link', 'set', 'lo', 'up')
        ns('ip', 'address', 'add', NS_IP + '/30', 'dev', NS_IF)
        ns('ip', 'link', 'set', NS_IF, 'up')
        # Из netns наружу только туннель и транспорт до entry: утечка мимо туннеля невозможна.
        for rule in (['-o', 'lo'], ['-o', 'vpn'], ['-d', HOST_IP],
                     ['-m', 'policy', '--dir', 'out', '--pol', 'ipsec']):
            ns('iptables', '-w', '-A', 'OUTPUT', *rule, '-j', 'ACCEPT')
        ns('iptables', '-w', '-P', 'OUTPUT', 'DROP')
        # ufw хоста пускает порты VPN, но не порты ретранслятора; правило с меткой.
        run('iptables', '-w', '-I', 'INPUT', '1', '-i', HOST_IF, '-m', 'comment', '--comment', COMMENT,
            '-j', 'ACCEPT')

    @staticmethod
    def source():
        """Адрес клиента внутри туннеля: им запрос подписан в API sing-box."""
        route = ns('ip', '-j', 'route', 'get', '192.0.2.1', check=False)
        try:
            src = json.loads(route.stdout)[0].get('prefsrc')
        except (ValueError, IndexError):
            return None
        return src if src and src != NS_IP else None


class Tap:
    """Счётчики на самом entry: пакеты с адреса проверки из туннеля и пакеты обратно на него.

    Запрос пришёл, а обратно не ушло ничего - не ответил сервер. Панель здесь ни при чём:
    ответ на снятый ею доступ всё равно прошёл бы POSTROUTING и был бы сосчитан.
    """
    RULES = (('raw', 'PREROUTING', '-s'), ('mangle', 'POSTROUTING', '-d'))

    def __init__(self, source):
        self.source = source
        self.unanswered = False

    def rule(self, action, table, chain, side):
        return run('iptables', '-w', '-t', table, action, chain, side, self.source,
                   '-m', 'comment', '--comment', COMMENT, check=False)

    def count(self, table, chain):
        listing = run('iptables', '-w', '-t', table, '-nvxL', chain, check=False).stdout
        for line in listing.splitlines():
            words = line.split()
            if f'/* {COMMENT} */' in line and self.source in words and words[0].isdigit():
                return int(words[0])
        return 0

    def __enter__(self):
        for table, chain, side in self.RULES if self.source else ():
            self.rule('-I', table, chain, side)
        return self

    def __exit__(self, *_error):
        if self.source:
            came, went = (self.count(table, chain) for table, chain, _side in self.RULES)
            self.unanswered = came > 0 and went == 0
            for table, chain, side in self.RULES:
                self.rule('-D', table, chain, side)


def clean_iptables():
    for table in ('filter', 'nat', 'mangle', 'raw'):
        dump = run('iptables-save', '-t', table, check=False).stdout
        for line in dump.splitlines():
            if line.startswith('-A ') and f'--comment {COMMENT}' in line:
                run('iptables', '-w', '-t', table, '-D', *line[3:].split(), check=False)


class WireGuard:
    name = 'WireGuard'

    def __init__(self):
        self.port = int(run('wg', 'show', 'wg0', 'listen-port').stdout)
        self.server = run('wg', 'show', 'wg0', 'public-key').stdout.strip()
        self.peers = {}
        self.ports = [('udp', self.port)]

    @staticmethod
    def free_addresses(count):
        iface = json.loads(run('ip', '-j', '-4', 'address', 'show', 'dev', 'wg0').stdout)[0]['addr_info'][0]
        network = ipaddress.ip_interface(f"{iface['local']}/{iface['prefixlen']}").network
        used = set(re.findall(r'(\d+\.\d+\.\d+\.\d+)/32', run('wg', 'show', 'wg0', 'allowed-ips').stdout))
        with contextlib.suppress(OSError):
            # Байты как у wg: latin-1 не падает на байте, который wg пропускает в комментарии.
            used |= set(re.findall(r'(\d+\.\d+\.\d+\.\d+)/32', WG_CONF.read_bytes().decode('latin-1')))
        free = [str(a) for a in reversed(list(network.hosts())) if str(a) not in used and str(a) != iface['local']]
        return free[:count]

    def issue(self, identity, address):
        private = run('wg', 'genkey').stdout.strip()
        public = run('wg', 'pubkey', data=private + '\n').stdout.strip()
        # Закрытый ключ только в памяти: на диске лишь открытый, по нему уборка снимает пира.
        (WORK / f'wg-{identity}.pub').write_text(public + '\n')
        self.peers[identity] = {'public': public, 'address': address, 'key': private, 'revoked': False}
        self.ensure(identity)

    def ensure(self, identity):
        """Панель раз в минуту делает syncconf и снимает чужих пиров: возвращаем своего."""
        peer = self.peers[identity]
        if not peer['revoked']:
            run('wg', 'set', 'wg0', 'peer', peer['public'], 'allowed-ips', peer['address'] + '/32')

    def connect(self, identity, endpoint):
        peer = self.peers[identity]
        self.down()
        ns('ip', 'link', 'add', 'vpn', 'type', 'wireguard')
        # Ключ через stdin: профиль AppArmor для wg не даёт читать файлы вне /etc/wireguard.
        ns('wg', 'set', 'vpn', 'private-key', '/dev/stdin', 'peer', self.server,
           'endpoint', f'{endpoint[0]}:{endpoint[1]}', 'allowed-ips', '0.0.0.0/0', 'persistent-keepalive', '1',
           data=peer['key'] + '\n')
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
        for path in WORK.glob('wg-*.pub'):
            run('wg', 'set', 'wg0', 'peer', path.read_text().strip(), 'remove', check=False)


class OpenVPN:
    name = 'OpenVPN'

    def __init__(self):
        conf = Path('/etc/openvpn/server/server.conf').read_text()
        self.port = int(re.search(r'^port\s+(\d+)', conf, re.M)[1])
        self.proto = 'tcp' if re.search(r'^proto\s+tcp', conf, re.M) else 'udp'
        self.ports = [(self.proto, self.port)]
        self.process = None
        self.names = {}

    def issue(self, identity, _address):
        name = f'vpn2-verify-{secrets.token_hex(4)}-{identity}'
        key, csr, crt, ext = (WORK / f'ovpn-{identity}{s}' for s in ('.key', '.csr', '.crt', '.ext'))
        run('openssl', 'req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
            '-subj', '/CN=' + name, '-keyout', str(key), '-out', str(csr))
        ext.write_text('basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=clientAuth\n')
        # Без `openssl ca`: index, serial и CRL панели не меняются. Срок - сутки, ключ только в /run.
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

    def connect(self, identity, endpoint):
        self.down()
        proto = 'udp' if self.proto == 'udp' else 'tcp-client'
        self.process = subprocess.Popen(
            ['ip', 'netns', 'exec', NS, 'openvpn', '--config', str(WORK / f'ovpn-{identity}.conf'),
             '--proto', proto, '--remote', endpoint[0], str(endpoint[1])],
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

    def cleanup(self):
        self.down()
        for path in CCD.glob('vpn2-verify-*'):
            path.unlink(missing_ok=True)


class IKEv2:
    name = 'IKEv2'

    def __init__(self):
        self.server_id = Path('/etc/anysda/ikev2-server-host').read_text().strip()
        if not re.fullmatch(r'[A-Za-z0-9.:-]+', self.server_id):
            raise ValueError('идентификатор сервера IKEv2')
        self.ports = [('udp', 500), ('udp', 4500)]
        self.secrets = {}
        self.process = None
        self.run_dir = WORK / 'charon'

    def server_conf(self):
        conns = ''.join(
            f'  vpn2-verify-{i} : connections.anysda-ikev2 {{\n'
            f'    remote-client {{\n      eap_id = "{s["id"]}"\n      groups =\n    }}\n  }}\n'
            for i, s in self.secrets.items() if not s['revoked'])
        creds = ''.join(f'  eap-vpn2-verify-{i} {{\n    id = "{s["id"]}"\n    secret = "{s["secret"]}"\n  }}\n'
                        for i, s in self.secrets.items() if not s['revoked'])
        SWAN_FILE.write_text(f'# Временный файл 99-verify, удаляется в конце проверки.\n'
                             f'connections {{\n{conns}}}\nsecrets {{\n{creds}}}\n')
        SWAN_FILE.chmod(0o600)
        run('swanctl', '--load-all', '--noprompt')

    def issue(self, identity, _address):
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

    def connect(self, identity, endpoint):
        self.down()
        self.start_charon()
        ns('ip', 'route', 'replace', 'default', 'via', HOST_IP)
        # AppArmor пускает swanctl только в /etc/swanctl: клиентский каталог подставляется
        # туда bind-монтированием в своём mount-namespace, серверный не меняется.
        client = WORK / 'swanctl'
        (client / 'x509').mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile('/etc/swanctl/x509/anysda-server.crt', client / 'x509/anysda-server.crt')
        secret = self.secrets[identity]
        # Порт 500 - обычный путь; иной порт (ретранслятор) - сразу NAT-T с порта 4500.
        ports = '' if endpoint[1] == 500 else f'    local_port = 4500\n    remote_port = {endpoint[1]}\n'
        conf = client / 'swanctl.conf'
        conf.write_text(
            f'connections {{\n  vpn {{\n    version = 2\n    remote_addrs = {endpoint[0]}\n{ports}'
            '    vips = 0.0.0.0\n    encap = yes\n'
            '    proposals = aes256gcm16-prfsha384-ecp384\n'
            f'    local {{\n      auth = eap-mschapv2\n      id = "{secret["id"]}"\n      eap_id = "{secret["id"]}"\n    }}\n'
            f'    remote {{\n      auth = pubkey\n      id = {self.server_id}\n      certs = anysda-server.crt\n    }}\n'
            '    children {\n      vpn {\n        remote_ts = 0.0.0.0/0\n'
            '        esp_proposals = aes256gcm16\n      }\n    }\n  }\n}\n'
            f'secrets {{\n  eap-client {{\n    id = "{secret["id"]}"\n    secret = "{secret["secret"]}"\n  }}\n}}\n')
        conf.chmod(0o600)
        run('unshare', '-m', 'sh', '-c', 'mount --bind "$0" /etc/swanctl && exec swanctl --load-all --noprompt '
            '--uri "$1"', str(client), self.vici())
        result = run('swanctl', '--initiate', '--child', 'vpn', '--uri', self.vici(), '--timeout', '15',
                     check=False, timeout=25)
        return result.returncode == 0 and Namespace.source() is not None

    def down(self):
        if self.process is not None and self.process.poll() is None:
            run('swanctl', '--terminate', '--ike', 'vpn', '--force', '--timeout', '5', '--uri', self.vici(),
                check=False)

    @staticmethod
    def server_sas(eap_id):
        """uniqueid живых IKE_SA сервера с этим EAP-логином, разбор как у продукта (vpn2.control.backend).

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
        """Подключение удалось, а сессии на сервере нет: её закрыла служба управления.

        Применяя список панели, она рвёт сессии логинов не из списка. Без удавшегося
        подключения отсутствие сессии ничего не говорит.
        """
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


def relay_reachable(relay, port, pauses=(0, 5, 10, 20)):
    """health ретранслятора с повтором: транспорт до экзита пропадает на десятки
    секунд, пока сторож не переключит его (hy2 -> tcp), и одиночный промах не
    значит, что внешней точки нет. Окно ~35 с пауз плюс до 13 с на попытку; на
    живом пути хватает первой.
    """
    for pause in pauses:
        time.sleep(pause)
        if relay.health(port):
            return True
    return False


class Relay:
    """Порт на veth хоста -> SOCKS5 tg-proxy -> экзит -> публичный адрес entry.

    Так клиент из netns стучится в порты entry снаружи, с адреса экзита, а не
    из локальной сети. tg-proxy есть на каждом entry: это inbound sing-box,
    его трафик к адресам самого entry уходит прямой ногой экзита (direct-best).
    """

    def __init__(self, wan_ip):
        self.wan_ip = wan_ip
        self.session = None

    @staticmethod
    def socks(command, address, port):
        sock = socket.create_connection(TG_PROXY, timeout=5)
        sock.sendall(b'\5\1\0')
        if sock.recv(2) != b'\5\0':
            raise OSError('socks auth')
        sock.sendall(b'\5' + bytes([command]) + b'\0\1' + socket.inet_aton(address) + struct.pack('!H', port))
        reply = sock.recv(10)
        if len(reply) < 10 or reply[1] != 0:
            raise OSError('socks reply')
        return sock, (socket.inet_ntoa(reply[4:8]), struct.unpack('!H', reply[8:10])[0])

    def health(self, port):
        """Эхо entry по публичному адресу через тот же SOCKS: экзит достаёт до entry снаружи.

        Без этого молчащий порт VPN нельзя отличить от сломанного пути экзит -> entry.
        """
        try:
            sock, _ = self.socks(1, self.wan_ip, port)
        except OSError:
            return False
        nonce = secrets.token_hex(16)
        with sock:
            sock.settimeout(8)
            sock.sendall(f'POST /vpn2-verify/{nonce} HTTP/1.0\r\nHost: verify\r\nContent-Length: 32\r\n\r\n{nonce}'
                         .encode())
            data = b''
            with contextlib.suppress(OSError):
                while chunk := sock.recv(4096):
                    data += chunk
        try:
            answer = json.loads(data.partition(b'\r\n\r\n')[2])
        except ValueError:
            return False
        # Запрос пришёл не с адреса самого entry: значит, снаружи, через экзит.
        return answer.get('nonce') == nonce and answer.get('local') is False

    def start(self, proto, port):
        """Новая сессия на свободном порту; прежняя закрывается вместе с её UDP-ассоциацией."""
        self.end_session()
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if proto == 'udp' else socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST_IP, 0))
        if proto == 'tcp':
            listener.listen(4)
            listener.settimeout(0.5)
        stop, sockets = threading.Event(), [listener]
        thread = threading.Thread(target=self.udp if proto == 'udp' else self.tcp,
                                  args=(listener, port, stop, sockets), daemon=True)
        self.session = stop, thread, sockets
        thread.start()
        return listener.getsockname()[1]

    def end_session(self):
        if self.session is None:
            return
        stop, thread, sockets = self.session
        self.session = None
        stop.set()
        thread.join(5)
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

    def udp(self, listener, port, stop, sockets):
        relay = client = None
        header = b'\0\0\0\1' + socket.inet_aton(self.wan_ip) + struct.pack('!H', port)
        while not stop.is_set():
            ready = select.select([s for s in (listener, relay) if s], [], [], 0.5)[0]
            try:
                if listener in ready:
                    data, client = listener.recvfrom(65535)
                    if relay is None:
                        control, address = self.socks(3, '0.0.0.0', 0)
                        sockets.append(control)
                        relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        sockets.append(relay)
                        relay.connect(('127.0.0.1', address[1]) if address[0] == '0.0.0.0' else address)
                    relay.send(header + data)
                if relay in ready and client:
                    data = relay.recv(65535)
                    if len(data) > 10 and data[3] == 1:
                        listener.sendto(data[10:], client)
            except OSError:
                stop.wait(0.2)

    def tcp(self, listener, port, stop, sockets):
        while not stop.is_set():
            try:
                inner, _ = listener.accept()
            except OSError:
                continue
            try:
                outer, _ = self.socks(1, self.wan_ip, port)
            except OSError:
                inner.close()
                continue
            sockets += [inner, outer]
            for a, b in ((inner, outer), (outer, inner)):
                threading.Thread(target=self.pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def pipe(src, dst):
        with contextlib.suppress(OSError):
            while data := src.recv(65536):
                dst.sendall(data)
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)

    def stop(self):
        self.end_session()


class ControlError(Exception):
    pass


def control_call(method, params):
    """Один запрос API службы управления; ошибка - код из ответа, без данных запроса."""
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(40)
        try:
            sock.connect(str(CONTROL_SOCKET))
            sock.sendall(json.dumps({'api': 1, 'method': method, 'params': params}).encode() + b'\n')
            reply = json.loads(sock.makefile('rb').readline(1024 * 1024 + 1))
        except (OSError, ValueError) as error:
            raise ControlError(f'служба не отвечает ({type(error).__name__})') from None
    if not isinstance(reply, dict) or reply.get('ok') is not True:
        raise ControlError(str(reply.get('error') if isinstance(reply, dict) else 'неверный ответ'))
    return reply.get('result')


def apply_peers(text):
    """Полный набор пиров в форме wg.apply или None, если его так не выразить."""
    peers = []
    for peer in conf_peers(text):
        ips = [a[:-3] for a in peer['allowed'] if re.fullmatch(r'\d+\.\d+\.\d+\.\d+/32', a)]
        if not peer['key'] or not peer['preshared_key'] or len(ips) != 1:
            return None
        peers.append({'key': peer['key'], 'preshared_key': peer['preshared_key'], 'ip': ips[0]})
    return peers


def save_file(path, name):
    """Копия файла (или отметка, что его нет) в WORK: обрыв посреди проверки восстановит уборка."""
    target = WORK / name
    if path.exists():
        shutil.copy2(path, target)
        stat = path.stat()
        (WORK / (name + '.owner')).write_text(f'{stat.st_uid}:{stat.st_gid}')
    else:
        (WORK / (name + '.absent')).write_text('')


def restore_file(path, name):
    saved, absent, owner = WORK / name, WORK / (name + '.absent'), WORK / (name + '.owner')
    if absent.exists():
        path.unlink(missing_ok=True)
    elif saved.exists():
        temporary = path.with_name('.vpn2-verify-' + path.name)
        shutil.copy2(saved, temporary)
        uid, gid = owner.read_text().split(':')
        os.chown(temporary, int(uid), int(gid))
        temporary.replace(path)
    for item in (saved, absent, owner):
        item.unlink(missing_ok=True)


def restore_wg_apply():
    """Возврат пиров wg0, файла wg0.conf и счётчика версий wg.apply к состоянию до проверки.

    Счётчик версий панели живёт в её базе: оставь проверка версию службы выше,
    следующий wg.apply панели получил бы stale.
    """
    if not (WORK / 'wg0.conf').exists():
        return
    # Файлы возвращаются и тогда, когда повторное чтение или syncconf не удались.
    try:
        # Интерфейс и пиры - какими их оставит wg после setconf сохранённого файла.
        interface, peers = wg_conf.read((WORK / 'wg0.conf').read_bytes())
        # Пустое значение wg отвергает: пир без PSK - без строки PresharedKey, без адресов - без
        # строки AllowedIPs (без неё syncconf очищает набор).
        body = '[Interface]\n' + ''.join(f'{name} = {value}\n' for name, value in interface.items()) + ''.join(
            f'\n[Peer]\nPublicKey = {peer["key"]}\n' +
            (f'PresharedKey = {peer["preshared_key"]}\n' if peer['preshared_key'] else '') +
            (f'AllowedIPs = {", ".join(peer["allowed"])}\n' if peer['allowed'] else '') for peer in peers)
        run('wg', 'syncconf', 'wg0', '/dev/stdin', data=body)
    finally:
        restore_file(WG_CONF, 'wg0.conf')
        restore_file(CONTROL_WG_STATE, 'control-wg.json')


def forget_wg_apply():
    """Сохранённые копии снимаются без возврата: на узле уже состояние панели, не проверки."""
    for name in ('wg0.conf', 'control-wg.json'):
        for suffix in ('', '.absent', '.owner'):
            (WORK / (name + suffix)).unlink(missing_ok=True)


def wg_apply_check(report):
    """Тестовый пир через wg.apply: виден в `wg show wg0`, затем убран тем же методом.

    Метод получает полный набор пиров, поэтому набор берётся из wg0.conf и
    сверяется с живым wg0. Панель может применить свой набор в те же секунды:
    до первого wg.apply проверки она проходит, и проверка начинает заново со
    свежего набора; после него панель получает stale или conflict и повторяет позже.
    """
    label, component = 'применение пира', 'WireGuard'
    for _ in range(4):
        moving = wg_apply_once(report, label, component)
        if not moving:
            return
        time.sleep(1.5)
    report.add(label, component, UNKNOWN, moving)


def wg_apply_once(report, label, component):
    """Одна попытка; непустой ответ - набор пиров сдвинула панель, строки отчёта ещё нет."""
    def done(status, reason):
        report.add(label, component, status, reason)
        return ''

    if not CONTROL_SOCKET.exists():
        return done(FAIL, 'нет сокета службы управления')
    try:
        text = WG_CONF.read_bytes()
        live = set(run('wg', 'show', 'wg0', 'peers').stdout.split())
    except (OSError, RuntimeError):
        return done(FAIL, 'нет wg0 или wg0.conf')
    try:
        peers = apply_peers(text)
    except ValueError as error:
        # Нечем спросить wg - не проверено; отказ wg - сбой.
        return done(UNKNOWN if 'проверить нечем' in str(error) else FAIL, str(error))
    if peers is None:
        return done(UNKNOWN, 'пиры wg0 расходятся с wg0.conf - полный набор для wg.apply неизвестен')
    if {peer['key'] for peer in peers} != live:
        # Служба управления посреди применения: пиры уже в wg0, файл ещё прежний.
        return 'пиры wg0 расходятся с wg0.conf - полный набор для wg.apply неизвестен'
    try:
        version = json.loads(CONTROL_WG_STATE.read_text())['version'] if CONTROL_WG_STATE.exists() else 0
    except (OSError, ValueError, KeyError, TypeError):
        return done(FAIL, 'состояние версий службы не читается')
    address = next(iter(WireGuard.free_addresses(1)), None)
    if not address:
        return done(UNKNOWN, 'нет свободного адреса в подсети wg0')
    save_file(WG_CONF, 'wg0.conf')
    save_file(CONTROL_WG_STATE, 'control-wg.json')
    private = run('wg', 'genkey').stdout.strip()
    test = {'key': run('wg', 'pubkey', data=private + '\n').stdout.strip(),
            'preshared_key': run('wg', 'genpsk').stdout.strip(), 'ip': address}
    try:
        try:
            control_call('wg.apply', {'version': version + 1, 'peers': peers + [test]})
        except ControlError as error:
            if str(error) in ('stale', 'conflict'):
                # Служба отказала до изменений: возвращать нечего.
                forget_wg_apply()
                return f'панель применяла пиров одновременно с проверкой, wg.apply отклонён: {error}'
            return done(FAIL, f'wg.apply с тестовым пиром отклонён: {error}')
        if test['key'] not in run('wg', 'show', 'wg0', 'peers').stdout.split():
            return done(FAIL, 'wg.apply ответил успехом, но пира нет в wg show wg0')
        try:
            control_call('wg.apply', {'version': version + 2, 'peers': peers})
        except ControlError as error:
            return done(FAIL, f'wg.apply без тестового пира отклонён: {error}')
        if test['key'] in run('wg', 'show', 'wg0', 'peers').stdout.split():
            return done(FAIL, 'тестовый пир остался в wg0 после wg.apply')
        return done(OK, 'тестовый пир применён через wg.apply, виден в wg0 и убран')
    finally:
        restore_wg_apply()


class TargetError(Exception):
    """Локальная цель эха не поднялась: без неё провал проб выглядел бы сломанным маршрутом."""


class Targets:
    READY_TIMEOUT = 20

    def __init__(self, wan_ip, exits, router):
        self.wan_ip = wan_ip
        self.port = self.new_port()
        self.local = None
        self.echo = None
        self.remote = {}
        self.failed = {}
        # Экзиты, где цель поднималась в любой попытке: kill ssh цель на экзите не гасит.
        self.reached = set()
        self.exit_ips = {tag: router.exit_ip(tag) for tag in exits}
        self.iface = router.outbounds.get(DIRECT, {}).get('bind_interface')

    def not_ready(self, process, err):
        """None - цель ответила «ready»; иначе процесс дождан, причина - код или предел и stderr."""
        if self.wait_ready(process, self.READY_TIMEOUT):
            return None
        try:
            reason = f'код {process.wait(timeout=2)}'
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            reason = f'не ответила за {self.READY_TIMEOUT} с'
        process.stdout.close()
        err.seek(0)
        # Последняя строка трассировки - сама причина (Address already in use и т. п.).
        tail = (err.read().strip().splitlines() or [''])[-1].strip()[-300:]
        return f'{reason}, stderr: {tail or "пусто"}'

    def local_target(self, listen, lifetime):
        # stderr - в файл: канал никто не читает, а причина отказа нужна оператору.
        with tempfile.TemporaryFile('w+') as err:
            process = subprocess.Popen([sys.executable, '-I', str(TARGET), '--listen', listen,
                                        '--port', str(self.port), '--lifetime', str(lifetime)],
                                       stdout=subprocess.PIPE, stderr=err, text=True)
            reason = self.not_ready(process, err)
        if reason is None:
            return process
        raise TargetError(f'локальная цель эха на {listen}:{self.port} не поднялась: {reason}')

    def exit_target(self, tag, lifetime):
        # Отказ ssh во входе и падение цели различимы только по коду и stderr.
        with tempfile.TemporaryFile('w+') as err:
            process = subprocess.Popen(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', tag,
                                        'bash', '-c', f"'{self.exit_command(self.port, lifetime)}'"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True)
            # ssh, которому отказали во входе, может выйти раньше, чем прочтёт скрипт.
            with contextlib.suppress(BrokenPipeError):
                process.stdin.write(TARGET.read_text())
            with contextlib.suppress(BrokenPipeError):
                process.stdin.close()
            reason = self.not_ready(process, err)
        if reason is None:
            self.remote[tag] = process
            self.reached.add(tag)
        else:
            self.failed[tag] = f'цель эха на экзите {tag} не поднялась: {reason}'

    @staticmethod
    def new_port():
        # Ниже ip_local_port_range (32768-60999): исходящие соединения экзита этот порт не займут.
        return 10000 + secrets.randbelow(22000)

    def echo_rule(self, action):
        return ('iptables', '-w', action, 'INPUT', *(('1',) if action == '-I' else ()), '-d', ECHO_IP,
                '-p', 'tcp', '--dport', str(self.port), '-m', 'comment', '--comment', COMMENT, '-j', 'ACCEPT')

    def start(self, lifetime, attempts=3):
        # Порт занят на каком-то экзите - все цели заново на новом порту: цель одна на весь прогон.
        for attempt in range(attempts):
            self.start_once(lifetime)
            if attempt + 1 == attempts or not any('Address already in use' in why
                                                  for why in self.failed.values()):
                return
            self.discard()
            self.port = self.new_port()

    def start_once(self, lifetime):
        # Эхо на адресе entry - только для ретранслятора (tg-proxy стоит выше блока адресов entry).
        self.local = self.local_target(self.wan_ip, lifetime)
        iface = self.iface or wan_device()
        if iface:
            run('ip', 'route', 'replace', 'local', ECHO_IP + '/32', 'dev', iface, 'table', 'local')
            run(*self.echo_rule('-I'))
            self.echo = self.local_target(ECHO_IP, lifetime)
        for tag, ip in self.exit_ips.items():
            if ip:
                self.exit_target(tag, lifetime)

    def discard(self):
        """Цели неудачной попытки гасятся здесь, кроме целей на экзитах.

        Цель на экзите без pty переживает kill своего ssh: её гасит остановка в stop().
        """
        for process in (self.local, self.echo, *self.remote.values()):
            if process:
                process.kill()
                process.wait()
                process.stdout.close()
        if self.echo:
            run(*self.echo_rule('-D'), check=False)
        self.local, self.echo, self.remote, self.failed = None, None, {}, {}

    @staticmethod
    def exit_command(port, lifetime):
        """Цель на экзите: локальный маршрут адреса эха и эхо на всех адресах (скрипт на stdin).

        Пустая строка первой: текст .bashrc без перевода строки не склеивается с «ready».
        """
        return (f'echo && ip route replace {EXIT_ECHO_ROUTE} && exec -a vpn2-verify-target python3 -I - '
                f'--listen 0.0.0.0 --port {port} --lifetime {lifetime}')

    @staticmethod
    def exit_stop_command():
        # Маршрут остаётся от прерванного запуска до следующей остановки: адрес документационный.
        return f'pkill -f "^vpn2-verify-target"; ip route del {EXIT_ECHO_ROUTE} 2>/dev/null; true'

    @staticmethod
    def wait_ready(process, timeout=20):
        # Строки до «ready» - текст, который .bashrc экзита печатает при входе по ssh.
        # Читаем дескриптор сами: строки из буфера файла select не видит.
        fd, data = process.stdout.fileno(), b''
        deadline = time.monotonic() + timeout
        while not any(line.strip() == b'ready' for line in data.split(b'\n')[:-1]):
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                return False
            chunk = os.read(fd, 4096)
            if not chunk:
                return False
            data += chunk
        return True

    def stop(self):
        for process in (self.local, self.echo):
            if process:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
        for tag in (tag for tag in self.exit_ips if tag in self.reached):
            run('ssh', '-n', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', tag,
                self.exit_stop_command(), check=False)
        for process in self.remote.values():
            process.kill()
            process.wait()


def manual_markers(exits, add):
    """Ручное правило: домен-метка в наборе pin-hy2-<экзит>-direct; sing-box перечитывает набор сам.

    Прямой маршрут - так же, меткой в наборе direct-ru (тег DIRECT): адрес entry
    может не входить в geoip-ru, а ручные наборы стоят выше гео-правил.
    """
    markers = {}
    for path in sorted(MANUAL_DIR.glob('*.json')) if MANUAL_DIR.is_dir() else []:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        rules = [r for r in data.get('rules', []) if not (isinstance(r, dict) and any(
            str(d).endswith(MARKER) for d in r.get('domain_suffix', [])))]
        tag = path.stem[len('pin-hy2-'):-len('-direct')] if path.stem.startswith('pin-hy2-') and \
            path.stem.endswith('-direct') else DIRECT if path.stem == DIRECT else None
        if add and tag in exits:
            markers[tag] = f'{secrets.token_hex(6)}.{tag}{MARKER}'
            rules.insert(0, {'domain_suffix': [markers[tag]]})
        if rules != data.get('rules', []):
            data['rules'] = rules
            temporary = path.with_name(path.name + '.vpn2-verify.tmp')
            temporary.write_text(json.dumps(data, indent=2))
            temporary.chmod(0o600)
            temporary.replace(path)
    return markers


def echo_addresses():
    """Локальные маршруты эха прямого пути."""
    try:
        routes = json.loads(run('ip', '-j', '-4', 'route', 'show', 'table', 'local', 'type', 'local',
                                ECHO_IP + '/32', check=False).stdout or '[]')
    except ValueError:
        return []
    return [['route', 'local', ECHO_IP + '/32', 'dev', entry['dev'], 'table', 'local']
            for entry in routes if entry.get('dev')]


def leftovers():
    found = []
    if echo_addresses():
        found.append('адрес эха')
    if run('ip', 'netns', 'list', check=False).stdout.split().count(NS):
        found.append('netns')
    if run('ip', 'link', 'show', HOST_IF, check=False).returncode == 0:
        found.append('veth')
    for table in ('filter', 'nat', 'mangle'):
        if f'--comment {COMMENT}' in run('iptables-save', '-t', table, check=False).stdout:
            found.append(f'iptables {table}')
    if SWAN_FILE.exists():
        found.append('conn IKEv2')
    if any(CCD.glob('vpn2-verify-*')):
        found.append('ccd OpenVPN')
    peers = run('wg', 'show', 'wg0', 'peers', check=False).stdout.split()
    for path in WORK.glob('wg-*.pub') if WORK.is_dir() else []:
        if path.read_text().strip() in peers:
            found.append('пир WireGuard')
    for path in MANUAL_DIR.glob('*.json') if MANUAL_DIR.is_dir() else []:
        if MARKER in path.read_text():
            found.append('ручное правило')
    return found


def cleanup(protocols=()):
    for protocol in protocols:
        with contextlib.suppress(OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            protocol.cleanup()
    # Прерванный прошлый запуск (SIGKILL, обрыв ssh): чистим по признакам, а не по журналу.
    for pid in run('ip', 'netns', 'pids', NS, check=False).stdout.split():
        with contextlib.suppress(ProcessLookupError, ValueError):
            os.kill(int(pid), signal.SIGKILL)
    # Ссылка опускается до удаления: иначе networkd-dispatcher пишет ошибку в журнал.
    if run('ip', 'link', 'set', HOST_IF, 'down', check=False).returncode == 0:
        time.sleep(1)
    run('ip', 'netns', 'del', NS, check=False)
    run('ip', 'link', 'del', HOST_IF, check=False)
    clean_iptables()
    for path in CCD.glob('vpn2-verify-*'):
        path.unlink(missing_ok=True)
    if SWAN_FILE.exists():
        SWAN_FILE.unlink()
        run('swanctl', '--load-all', '--noprompt', check=False)
    manual_markers((), add=False)
    if WORK.is_dir():
        with contextlib.suppress(OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            restore_wg_apply()
    for item in echo_addresses():
        run('ip', item[0], 'del', *item[1:], check=False)
    if WORK.is_dir():
        peers = run('wg', 'show', 'wg0', 'peers', check=False).stdout.split()
        for path in WORK.glob('wg-*.pub'):
            if path.read_text().strip() in peers:
                run('wg', 'set', 'wg0', 'peer', path.read_text().strip(), 'remove', check=False)
        shutil.rmtree(WORK, ignore_errors=True)


class Verifier:
    def __init__(self, report, exits, wan_ip, public_ip):
        self.report = report
        self.exits = exits
        self.wan_ip = wan_ip
        self.public_ip = public_ip
        self.router = Router()
        self.targets = Targets(wan_ip, exits, self.router)
        self.markers = {}
        self.internet = []
        self.dns_reference = {}
        self.relay = None
        self.relay_ok = False

    def prepare(self):
        self.targets.start(lifetime=900)
        self.markers = manual_markers(self.exits + [DIRECT], add=True)
        for host, path, status in INTERNET:
            ip = resolve(host)
            if ip:
                self.internet.append((ip, host, path, status))
        for server in ('10.99.0.1', '127.0.0.2'):
            self.dns_reference[server] = probe_dns(server, 'dns.google')
        if self.exits and self.targets.remote:
            self.relay = Relay(self.public_ip)
            # ufw не пускает случайный порт цели; правило с меткой снимает уборка.
            run('iptables', '-w', '-I', 'INPUT', '1', '-p', 'tcp', '--dport', str(self.targets.port),
                '-m', 'comment', '--comment', COMMENT, '-j', 'ACCEPT')
            self.relay_ok = relay_reachable(self.relay, self.targets.port)

    # Действующий доступ проверки: кто и куда подключён, отметка службы управления
    # перед подключением и удалось ли оно.
    link = None
    stamp = None
    linked = False

    def attach(self, protocol):
        """Одно подключение действующего доступа; перед ним на сервер возвращаются пиры проверки."""
        identity, endpoint = self.link
        self.stamp = applied()
        if hasattr(protocol, 'ensure'):
            for peer in protocol.peers:
                protocol.ensure(peer)
        self.linked = bool(protocol.connect(identity, endpoint))
        return self.linked

    def moved(self, protocol):
        """След панели: служба управления применяла список после подключения или доступ пропал с сервера."""
        if self.link is None:
            return False
        lost = getattr(protocol, 'lost', None)
        return applied() != self.stamp or bool(lost and lost(self.link[0], self.linked))

    def connect(self, protocol, identity, endpoint):
        """Подключение действующего доступа. Помешала панель - повтор, пока она не оставит попытку в покое."""
        self.report.blurred = False
        self.link = (identity, endpoint)
        for _ in range(RESTORES + 1):
            if self.attach(protocol):
                return True
            if not self.moved(protocol):
                return False
        self.report.blurred = True
        return False

    def probe(self, protocol, kind, args, dest=None, retry=True):
        result, seen = in_namespace(self.router, Namespace.source(), kind, args, dest)
        if result or not retry:
            return result, seen
        self.report.blurred = False
        # Панель выпустила или удалила устройство и применила свой список: временный
        # пир снят, сессия закрыта. Это видно по службе управления, а не угадывается:
        # без следа панели неудача пробы остаётся неудачей.
        for _ in range(RESTORES):
            if not self.moved(protocol):
                break
            self.attach(protocol)
            source = Namespace.source()
            with Tap(source) as tap:
                result, seen = in_namespace(self.router, source, kind, args, dest)
            if result:
                return result, seen
            # Запрос до сервера дошёл, ответа сервер не дал: поломка настоящая, как бы часто
            # панель ни меняла список. Не дошёл запрос или пропал ответ - попытка не в счёт.
            if tap.unanswered:
                return result, seen
        else:
            if self.moved(protocol):
                self.report.blurred = True
                return result, seen
        if hasattr(protocol, 'ensure'):
            for identity in protocol.peers:
                protocol.ensure(identity)
            time.sleep(2)
            result, seen = in_namespace(self.router, Namespace.source(), kind, args, dest)
        return result, seen

    def paths(self, protocol):
        name = protocol.name
        port = self.targets.port
        # Адрес эха не входит в geoip-ru: в прямой выход его уводит метка в
        # ручном наборе direct-ru. Набор перечитывается не мгновенно.
        if not self.direct_target():
            self.report.add('маршрут: прямой', name, UNKNOWN, self.direct_missing())
        else:
            for _ in range(4):
                result, seen = self.probe(protocol, 'echo', self.echo_args(), (ECHO_IP, port))
                leaves = {s['chains'][0] for s in seen if s['chains']}
                if result and result['local'] and leaves == {DIRECT} or self.report.blurred:
                    break
                time.sleep(3)
            if result and result['local'] and leaves == {DIRECT}:
                self.report.add('маршрут: прямой', name, OK, f'эхо entry ответило через выход {DIRECT}')
            elif result and not seen:
                self.report.add('маршрут: прямой', name, FAIL, 'ответ пришёл, но запрос не прошёл через sing-box')
            else:
                self.report.add('маршрут: прямой', name, FAIL,
                                f'нет эха или неверный выход: {sorted(leaves) or "нет соединения"}')
        for tag in self.exits:
            if tag not in self.targets.remote or tag not in self.markers:
                why = self.targets.failed.get(tag, 'нет эха на экзите (ssh)') \
                    if tag not in self.targets.remote else 'нет набора pin-hy2-*-direct'
                self.report.add(f'маршрут: через экзит {tag}', name, UNKNOWN, why)
                continue
            expected = f'hy2-{tag}-direct'
            # Метка ручного маршрута свежая: sing-box перечитывает набор правил не мгновенно.
            for _ in range(4):
                result, seen = self.probe(protocol, 'echo', [EXIT_ECHO_IP, port, self.markers[tag]],
                                          (EXIT_ECHO_IP, port))
                leaves = {s['chains'][0] for s in seen if s['chains']}
                rules = {s['rule'] + ' ' + s['payload'] for s in seen}
                path_ok = bool(result) and result['local'] and leaves == {expected}
                if path_ok or self.report.blurred:
                    break
                time.sleep(3)
            blurred = self.report.blurred  # обе строки ниже - об одной и той же пробе
            self.report.add(f'маршрут: через экзит {tag}', name, OK if path_ok else FAIL,
                            f'эхо экзита ответило с самого экзита через {expected}' if path_ok else
                            f'нет эха экзита или неверный выход: {sorted(leaves) or "нет соединения"}')
            manual = any(f'manual-pin-hy2-{tag}-direct' in r for r in rules)
            self.report.blurred = blurred
            self.report.add(f'ручное правило {tag}', name, OK if path_ok and manual else FAIL,
                            'запрос выбрал ручной маршрут панели' if path_ok and manual else
                            'ручной маршрут не сработал')
        self.egress_check(protocol)
        self.internet_check(protocol)

    def egress_check(self, protocol):
        """Выход наружу через каждый экзит отдельно.

        Эхо маршрута отвечает с самого экзита и проверяет только туннель до него, а
        «интернет через экзит» принимает любой экзит. Здесь внешний адрес запрашивается
        с меткой ручного маршрута экзита: ответ возможен, только если наружу вышел он.
        """
        name = protocol.name
        for tag in self.exits:
            label = f'интернет через экзит {tag}'
            expected = f'hy2-{tag}-direct'
            if tag not in self.markers:
                self.report.add(label, name, UNKNOWN, 'нет набора pin-hy2-*-direct')
                continue
            for ip, _host, _path, _status in self.internet:
                result, seen = self.probe(protocol, 'egress', [ip, self.markers[tag]], (ip, 80))
                leaves = {s['chains'][0] for s in seen if s['chains']}
                if result and leaves == {expected}:
                    self.report.add(label, name, OK, f'внешний адрес ответил через {expected}')
                    break
                if result:
                    self.report.add(label, name, FAIL, f'внешний адрес ответил мимо экзита: '
                                    f'{sorted(leaves) or "нет соединения в sing-box"}')
                    break
            else:
                reachable = [host for ip, host, path, status in self.internet if probe_internet(ip, host, path, status)]
                if reachable:
                    self.report.add(label, name, FAIL, f'с entry {reachable[0]} открывается, '
                                    f'через экзит {tag} внешние адреса не отвечают')
                else:
                    self.report.add(label, name, UNKNOWN,
                                    'внешние адреса недоступны и с самого entry - причина неразличима')
        self.dns_check(protocol)

    def internet_check(self, protocol):
        name = protocol.name
        if not self.exits:
            self.report.add('интернет через экзит', name, NOT_NEEDED, 'экзитов нет')
            return
        evidence = []
        for ip, host, path, status in self.internet:
            result, seen = self.probe(protocol, 'internet', [ip, host, path, status], (ip, 80))
            leaves = {s['chains'][0] for s in seen if s['chains']}
            evidence.append(sorted(leaves))
            if result and leaves and all(leaf.startswith('hy2-') for leaf in leaves):
                self.report.add('интернет через экзит', name, OK, f'{host} ответил через {sorted(leaves)[0]}')
                return
            if result:
                self.report.add('интернет через экзит', name, FAIL,
                                f'{host} ответил мимо экзита: {sorted(leaves) or "нет соединения в sing-box"}')
                return
        # Эталон с самого entry: недоступен и отсюда - сбой внешнего адреса, а не VPN.
        reachable = [host for ip, host, path, status in self.internet if probe_internet(ip, host, path, status)]
        if reachable:
            self.report.add('интернет через экзит', name, FAIL,
                            f'с entry напрямую {reachable[0]} открывается, через экзит - нет')
        else:
            self.report.add('интернет через экзит', name, UNKNOWN,
                            'внешние адреса недоступны и с самого entry - причина неразличима')

    def dns_check(self, protocol):
        name = protocol.name
        for server, label, chain in (('10.99.0.1', 'DNS AdGuard', None), ('8.8.8.8', 'DNS внешний', 'dns-unfiltered')):
            reference = self.dns_reference.get('10.99.0.1' if chain is None else '127.0.0.2')
            # sing-box перехватывает DNS и показывает назначением свой адрес: сверяем только порт.
            result, seen = self.probe(protocol, 'dns', [server, 'dns.google'], (None, 53) if chain else None)
            if chain is None:
                seen = []
            leaves = {s['chains'][0] for s in seen if s['chains']}
            if not reference:
                self.report.add(label, name, UNKNOWN, 'нет эталонного ответа резолвера с самого entry')
            elif result and set(result) & set(reference) and (chain is None or leaves == {chain}):
                self.report.add(label, name, OK, f'ответ совпал с эталоном{" через " + chain if chain else ""}')
            else:
                self.report.add(label, name, FAIL,
                                f'нет ответа или чужой путь: {sorted(leaves) if chain else "нет ответа"}')

    def negative(self, protocol):
        protocol.down()
        leaks = []
        for kind, args in (('echo', self.echo_args()),
                           ('dns', ['10.99.0.1', 'dns.google'])):
            result, _ = self.probe(protocol, kind, args, retry=False)
            if result:
                leaks.append(kind)
        self.report.add('отрицательный контроль', protocol.name, FAIL if leaks else OK,
                        f'при опущенном туннеле прошли: {", ".join(leaks)}' if leaks else
                        'туннель опущен - эхо и DNS недоступны')

    def direct_target(self):
        return DIRECT in self.markers and self.targets.echo is not None

    def direct_missing(self):
        return f'нет ручного набора {DIRECT}' if DIRECT not in self.markers else \
            f'нет интерфейса выхода {DIRECT} для эха'

    def echo_args(self):
        return [ECHO_IP, self.targets.port, self.markers.get(DIRECT, 'direct' + MARKER)]

    def echo_ok(self, protocol, retry=True):
        result, _ = self.probe(protocol, 'echo', self.echo_args(), retry=retry)
        return bool(result)

    def revocation(self, protocol):
        name = protocol.name
        if not self.connect(protocol, 'a', (HOST_IP, protocol.ports[0][1])) or not self.echo_ok(protocol):
            self.report.add('отзыв', name, FAIL, 'повторное подключение старого доступа до отзыва не удалось')
            return False
        protocol.revoke('a')
        time.sleep(1)
        live = self.echo_ok(protocol, retry=False)
        again = protocol.connect('a', (HOST_IP, protocol.ports[0][1])) and self.echo_ok(protocol, retry=False)
        protocol.issue('b', self.addresses[1] if self.addresses else None)
        fresh = self.connect(protocol, 'b', (HOST_IP, protocol.ports[0][1])) and self.echo_ok(protocol)
        if not live and not again and fresh:
            self.report.add('отзыв', name, OK, 'старый доступ и его сессия закрыты, новый передаёт данные')
        else:
            self.report.add('отзыв', name, FAIL, 'после отзыва: ' + ', '.join(
                text for bad, text in ((live, 'живая сессия работает'), (again, 'старый доступ подключается'),
                                       (not fresh, 'действующий доступ не работает')) if bad))
        return fresh

    def external(self, protocol):
        name = protocol.name
        if not self.exits:
            self.report.add('внешняя проба портов', name, UNKNOWN, 'нет экзита - нет внешней точки измерения')
            return
        if not self.relay or not self.relay_ok:
            self.report.add('внешняя проба портов', name, UNKNOWN,
                            'экзит не достаёт до публичного адреса entry (tg-proxy) - нет внешней точки измерения')
            return
        proto, port = protocol.ports[-1]
        # Каждая попытка - своя сессия ретранслятора: UDP-ассоциация SOCKS5 бывает мёртвой с начала.
        for pause in (0, 15, 45):
            time.sleep(pause)
            relay_port = self.relay.start(proto, port)
            connected = self.connect(protocol, 'b', (HOST_IP, relay_port)) and self.echo_ok(protocol)
            if connected or self.report.blurred:
                break
        self.report.add('внешняя проба портов', name, OK if connected else FAIL,
                        f'{proto}/{port} открыт с адреса экзита, туннель передаёт данные' if connected else
                        f'{proto}/{port} не отвечает с адреса экзита (локально работает)')

    def protocol(self, protocol):
        name = protocol.name
        self.addresses = protocol.free_addresses(2) if hasattr(protocol, 'free_addresses') else []
        protocol.issue('a', self.addresses[0] if self.addresses else None)
        connected = self.connect(protocol, 'a', (HOST_IP, protocol.ports[0][1]))
        source = Namespace.source() if connected else None
        if not connected or not source:
            self.report.add('подключение', name, FAIL, f'клиент не подключился к {protocol.ports[0][0]}/'
                            f'{protocol.ports[0][1]}')
            for check in ('маршруты, DNS, отзыв', 'внешняя проба портов'):
                self.report.add(check, name, UNKNOWN, 'нет подключения')
            return
        self.report.add('подключение', name, OK, 'временный клиент подключён и получил адрес в туннеле')
        self.paths(protocol)
        self.negative(protocol)
        if not self.direct_target():
            # Без эха в прямом выходе живой туннель неотличим от мёртвого.
            for check in ('отзыв', 'внешняя проба портов'):
                self.report.add(check, name, UNKNOWN, self.direct_missing())
        elif self.revocation(protocol):
            self.external(protocol)
        else:
            self.report.add('внешняя проба портов', name, UNKNOWN, 'нет действующего доступа')


def default_route(field):
    route = run('ip', '-j', '-4', 'route', 'get', '192.0.2.1', check=False)
    try:
        return json.loads(route.stdout)[0][field]
    except (ValueError, IndexError, KeyError):
        return None


def detect_wan():
    return default_route('prefsrc')


def wan_device():
    return default_route('dev')


def verify(report, exits, public_host):
    wan_ip = detect_wan()
    public_ip = resolve(public_host) if public_host else wan_ip
    if not wan_ip or not public_ip:
        report.add('подготовка', 'контрольный клиент', FAIL, 'не найден внешний адрес entry')
        return
    protocols = []
    verifier = None
    try:
        cleanup()
        WORK.mkdir(mode=0o700, parents=True, exist_ok=True)
        Namespace().up()
        protocols = [WireGuard(), OpenVPN(), IKEv2()]
        verifier = Verifier(report, exits, wan_ip, public_ip)
        try:
            verifier.prepare()
        except TargetError as error:
            report.add('подготовка', 'цель эха', FAIL, str(error))
            return
        for protocol in protocols:
            try:
                verifier.protocol(protocol)
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
                report.add('исполнение', protocol.name, FAIL, f'проверка прервана: {error}')
            finally:
                protocol.cleanup()
        try:
            wg_apply_check(report)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
            report.add('применение пира', 'WireGuard', FAIL, f'проверка прервана: {error}')
    finally:
        if verifier:
            if verifier.relay:
                verifier.relay.stop()
            verifier.targets.stop()
        cleanup(protocols)
        rest = leftovers()
        report.add('восстановление', 'очистка', FAIL if rest else OK,
                   'остались: ' + ', '.join(rest) if rest else
                   'временные реквизиты, netns, правила и метки убраны')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    probe = sub.add_parser('probe')
    probe.add_argument('kind', choices=sorted(PROBES))
    probe.add_argument('args')
    check = sub.add_parser('check')
    check.add_argument('--tag', default='ru')
    check.add_argument('--exits', default='')
    check.add_argument('--public-host', default='')
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
        verify(report, args.exits.split(), args.public_host)
    if args.report:
        with open(args.report, 'a') as rows:
            for row in report.rows:
                rows.write(json.dumps(row, ensure_ascii=False) + '\n')
    return report.code()


if __name__ == '__main__':
    sys.exit(main())
