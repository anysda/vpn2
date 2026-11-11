"""Шаги VPN-серверов entry для ролей wireguard, openvpn и ikev2."""
import os
import re
import subprocess
import time
from pathlib import Path

from vpn2 import pki, wg

WG_DIR = '/etc/wireguard'
WG_ULA = 'fd66:66::1'
WG_NOTE = 'Initial config — panel will overwrite via syncWireguardConfig().'
# Приватный ключ WG - 32 байта в base64.
WG_PRIVATE = r'[A-Za-z0-9+/]{43}='

OVPN_DIR = '/etc/openvpn/server'
OVPN_UNIT = 'openvpn-server@server'
OVPN_PKI = f'{OVPN_DIR}/pki'
OVPN_DIRS = (f'{OVPN_DIR}/ccd', '/run/openvpn-server')
OVPN_API_MANAGED = '/etc/vpn2-control/api-managed'
OVPN_APPLY = '/usr/local/sbin/vpn2-apply'
OVPN_CONF = '''port {port}
proto {proto}
dev tun0
topology subnet
# Постоянные адреса устройств .2-.239 панель раздаёт через ccd
# (ifconfig-push). Общий пул только .240-.253 для устройств, которым панель
# ещё не выдала адрес, чтобы он не пересекался с закреплёнными (VPN2-115).
server {subnet} 255.255.255.0 nopool
ifconfig-pool {base}.240 {base}.253 255.255.255.0
ca   {pki}/ca.crt
cert {pki}/server.crt
key  {pki}/server.key
dh none
tls-crypt {pki}/tls-crypt.key
crl-verify {pki}/crl.pem
client-config-dir {directory}/ccd
# keepalive 5 30: пуш клиентам ping 5 / ping-restart 30 — детект мёртвого
# entry за ~30с и быстрый реконнект (было 10/60 → ~62с простоя после ребута).
keepalive 5 30
cipher AES-256-GCM
data-ciphers AES-256-GCM:CHACHA20-POLY1305
auth SHA256
persist-key
persist-tun
# Буферы сокета системные (0 - не трогать, после 00-bootstrap это 8 МиБ);
# fast-io - запись в UDP без лишнего poll, на TCP опции нет.
sndbuf 0
rcvbuf 0
{fastio}
user nobody
group nogroup
push "redirect-gateway def1 bypass-dhcp"
# DNS is NOT pushed from the server — each client's .ovpn carries its own
# dhcp-option DNS (AdGuard or public, per the panel's filterTraffic flag).
# status-version 2 → машиночитаемый CSV (CLIENT_LIST,...); файл лежит в
# /etc/openvpn/server (этот путь смонтирован в контейнер панели) — сборщик
# трафика панели читает отсюда per-client байты.
status {directory}/status-server.log
status-version 2
# Management на unix-сокете, не TCP. Через него панель рвёт уже установленные
# сессии при заморозке, истечении, удалении и перевыпуске: ccd-директиву disable и CRL
# OpenVPN проверяет только на НОВОМ подключении (VPN2-39). Сокет OpenVPN
# создаёт с правами 0777: разрешён только управляющий UID. До первой миграции
# остаётся root; стадия панели транзакционно переключает его на сервис.
management {directory}/mgmt.sock unix
management-client-user {user}
verb 3
'''


def dns_rules(destination, subnets):
    """Правила ufw: клиенты пулов ходят к DNS на destination по udp и tcp."""
    return [f'proto {proto} from {subnet} to {destination} port 53'
            for subnet in subnets for proto in ('udp', 'tcp')]


def _filled(path):
    try:
        return os.stat(path).st_size > 0
    except OSError:
        return False


def _text(path):
    try:
        return Path(path).read_text(errors='replace')
    except OSError:
        return ''


def _wg_state(root):
    names = ('keys/server.priv', 'keys/server.pub', 'wg0.conf')
    links = [os.readlink(root / name) if (root / name).is_symlink() else None
             for name in ('server.priv', 'server.pub')]
    return [(root / name).read_bytes() if (root / name).is_file() else None for name in names], links


def wg_identity(port, address, *, root=WG_DIR, run=wg.run):
    """Ключи сервера в keys/, ссылки на них и начальный wg0.conf; True - что-то сменилось.

    Каталог root целиком не заменяется: это bind mount панели с wg0.conf. Действующий
    приватный ключ не ротируется: негодный файл при живых server.pub или wg0.conf - отказ.
    """
    root = Path(root)
    conf = root / 'wg0.conf'
    before = _wg_state(root)
    with pki.transaction(root / 'keys') as work:
        private, public = work / 'server.priv', work / 'server.pub'
        # Миграция плоской раскладки: приватный ключ не меняется.
        if not _filled(private) and _filled(root / 'server.priv'):
            private.write_bytes((root / 'server.priv').read_bytes())
        if not _filled(private) and _filled(conf):
            key = wg.private_key(_text(conf))
            private.write_text(f'{key}\n' if key else '')
        key = _text(private).rstrip('\n')
        if not re.fullmatch(WG_PRIVATE, key, re.ASCII):
            if _filled(root / 'server.pub') or _filled(conf):
                raise pki.PkiIdentity('PKI: cannot replace an active WireGuard private key')
            key = wg.genkey(run=run)
            private.write_text(key + '\n')
        public.write_text(wg.pubkey(key, run=run) + '\n')
        private.chmod(0o600)
        public.chmod(0o600)
    for name in ('server.priv', 'server.pub'):
        link = root / f'.{name}.link'
        link.unlink(missing_ok=True)
        os.symlink(f'keys/{name}', link)
        os.replace(link, root / name)
    if not conf.is_file():
        fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(wg.render_interface([f'{address}/24', f'{WG_ULA}/64'], port, key, note=WG_NOTE))
    return _wg_state(root) != before


def _quiet(run, argv):
    return run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def wg_live_addresses(*, run=subprocess.run):
    """Глобальные адреса живого wg0, как их печатает `ip -o addr`."""
    done = run(['ip', '-o', 'addr', 'show', 'dev', 'wg0', 'scope', 'global'], text=True,
               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return [line.split()[3] for line in done.stdout.splitlines() if len(line.split()) > 3]


def wg_apply(say, *, conf=wg.CONF_PATH, run=subprocess.run):
    """wg0.conf в ядро: `wg syncconf` при прежних адресах, иначе рестарт wg-quick.

    Рестарт рвёт туннель всем клиентам, а на повторном деплое конфиг обычно прежний.
    True - служба перезапущена.
    """
    if (_quiet(run, ['systemctl', 'is-active', '--quiet', 'wg-quick@wg0'])
            and _quiet(run, ['ip', 'link', 'show', 'wg0'])
            and sorted(wg.addresses(_text(conf))) == sorted(wg_live_addresses(run=run))):
        body = run(['wg-quick', 'strip', 'wg0'], check=True, stdout=subprocess.PIPE).stdout
        run(['wg', 'syncconf', 'wg0', '/dev/stdin'], input=body, check=True)
        say('адреса wg0 прежние — wg syncconf без рестарта')
        return False
    run(['systemctl', 'restart', 'wg-quick@wg0'], check=True)
    say('wg-quick@wg0 перезапущен')
    return True


def routing_ensure(unit, iface, chain, say, *, run=subprocess.run):
    """Цепочка TPROXY службы пропала при живой службе - перезапуск её юнита."""
    if _quiet(run, ['iptables', '-t', 'mangle', '-C', 'PREROUTING', '-i', iface, '-j', chain]):
        return False
    say(f'{unit}: цепочка {chain} пропала, восстанавливаю')
    run(['systemctl', 'restart', unit], check=True)
    return True


def ovpn_protocols(proto):
    """OVPN_PROTO оператора -> (proto в server.conf, протокол правила ufw).

    OpenVPN различает udp4/udp6 и tcp[46]-server, ufw принимает только udp и tcp.
    """
    if proto in ('udp', 'udp4', 'udp6', 'tcp-server', 'tcp4-server', 'tcp6-server'):
        return proto, proto[:3]
    if proto in ('tcp', 'tcp4', 'tcp6'):
        return f'{proto}-server', 'tcp'
    raise ValueError(f'unsupported OpenVPN protocol: {proto}')


def ovpn_server_conf(port, proto, subnet, subnets, user, *, directory=OVPN_DIR):
    """Текст server.conf; subnets - все подсети пула, user - кому открыт management.

    tun0 и адреса прежних клиентов остаются /24: дополнительные подсети пула
    маршрутизируются в тот же tun0.
    """
    server_proto, _ = ovpn_protocols(proto)
    text = OVPN_CONF.format(port=port, proto=server_proto, subnet=subnet, base=subnet.rsplit('.', 1)[0],
                            pki=f'{directory}/pki', directory=directory, user=user,
                            fastio='fast-io' if proto.startswith('udp') else '')
    return text + ''.join(f'route {net.split("/")[0]} 255.255.255.0\n'
                          for net in subnets if net != f'{subnet}/24')


def ovpn_control_user(marker=OVPN_API_MANAGED):
    """Кому открыт management-сокет: до первой миграции панели root, после неё служба."""
    return 'vpn2-control' if os.path.isfile(marker) else 'root'


def ovpn_write_conf(port, proto, subnet, subnets, *, directory=OVPN_DIR, marker=OVPN_API_MANAGED):
    """server.conf на диске; True - текст сменился."""
    path = Path(directory) / 'server.conf'
    text = ovpn_server_conf(port, proto, subnet, subnets, ovpn_control_user(marker), directory=directory)
    if _text(path) == text and path.is_file():
        return False
    path.write_text(text)
    return True


def _tree(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in sorted(Path(directory).rglob('*')) if path.is_file()}


def ovpn_pki(*, target=OVPN_PKI, dirs=OVPN_DIRS, run=pki.run):
    """Комплект PKI в target одной подменой и каталоги dirs; True - комплект сменился."""
    target = Path(target)
    for path in (target / 'newcerts', *map(Path, dirs)):
        path.mkdir(parents=True, exist_ok=True)
    before = _tree(target)
    with pki.transaction(target) as work:
        pki.build_openvpn(work, run=run)
    return _tree(target) != before


def ovpn_inputs(directory=OVPN_DIR):
    """Входы openvpn-server@server; тот же список у ovpn_inputs в anysda-svc.sh.

    CRL и ccd OpenVPN перечитывает сам на каждом подключении, их в отпечатке нет.
    """
    return ['/usr/sbin/openvpn', '/usr/lib/systemd/system/openvpn-server@.service',
            *(f'{directory}/{name}' for name in ('server.conf', 'pki/ca.crt', 'pki/server.crt',
                                                 'pki/server.key', 'pki/tls-crypt.key'))]


def ovpn_restart(restart, say, *, apply=OVPN_APPLY, run=subprocess.run, sleep=time.sleep):
    """Рестарт сервера при смене входов, проба запуска и отметка last good.

    restart() - перезапуск по отпечатку входов, True при рестарте (он рвёт всех клиентов).
    Поднявшийся server.conf и есть last good: иначе откат деплоя, упавшего до стадии
    control, вернул бы прежние порт, шифры и пулы. Первую установку, пока vpn2-apply
    нет, отмечает стадия control. Сервер не поднялся - RuntimeError.
    """
    changed = restart()
    say(f'{OVPN_UNIT} перезапущен' if changed
        else f'{OVPN_UNIT}: конфиг, сертификаты и бинарник прежние — не перезапускаю')
    active = ['systemctl', 'is-active', '--quiet', OVPN_UNIT]
    # Type=notify: restart вернулся после READY=1, проба только ловит падение.
    for _ in range(10 if changed else 1):
        if _quiet(run, active):
            break
        sleep(0.2)
    else:
        raise RuntimeError(f'{OVPN_UNIT} не поднялся')
    if os.access(apply, os.X_OK) and run([apply, 'mark', 'openvpn']).returncode != 0:
        say('vpn2-apply mark openvpn не прошёл: last good не совпадает с server.conf')
    return changed
