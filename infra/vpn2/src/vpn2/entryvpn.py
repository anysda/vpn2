"""Шаги VPN-серверов entry для ролей wireguard, openvpn и ikev2."""
import os
import re
import subprocess
import time
from pathlib import Path

from vpn2 import pki, wg
from vpn2.fsutil import filled

WG_DIR = '/etc/wireguard'
WG_ULA = 'fd66:66::1'
WG_NOTE = 'Initial config — panel will overwrite via syncWireguardConfig().'
# Приватный ключ WG - 32 байта в base64.
WG_PRIVATE = r'[A-Za-z0-9+/]{43}='

OVPN_DIR = '/etc/openvpn/server'
OVPN_UNIT = 'openvpn-server@server'
OVPN_PKI = f'{OVPN_DIR}/pki'
OVPN_DIRS = (f'{OVPN_DIR}/ccd', '/run/openvpn-server')
OVPN_APPLY = '/usr/local/sbin/vpn2-apply'


def dns_rules(destination, subnets):
    """Правила ufw: клиенты пулов ходят к DNS на destination по udp и tcp."""
    return [f'proto {proto} from {subnet} to {destination} port 53'
            for subnet in subnets for proto in ('udp', 'tcp')]


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
        if not filled(private) and filled(conf):
            key = wg.private_key(_text(conf))
            private.write_text(f'{key}\n' if key else '')
        key = _text(private).rstrip('\n')
        if not re.fullmatch(WG_PRIVATE, key, re.ASCII):
            if filled(root / 'server.pub') or filled(conf):
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


def wg_apply(say, *, conf=wg.CONF_PATH, run=subprocess.run):
    """wg0.conf в ядро: `wg syncconf` при прежних адресах, иначе рестарт wg-quick.

    Рестарт рвёт туннель всем клиентам, а на повторном деплое конфиг обычно прежний.
    True - служба перезапущена.
    """
    live = run(['ip', '-o', 'addr', 'show', 'dev', 'wg0', 'scope', 'global'], text=True,
               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.splitlines()
    if (_quiet(run, ['systemctl', 'is-active', '--quiet', 'wg-quick@wg0'])
            and _quiet(run, ['ip', 'link', 'show', 'wg0'])
            and sorted(wg.addresses(_text(conf))) == sorted(row.split()[3] for row in live if len(row.split()) > 3)):
        body = run(['wg-quick', 'strip', 'wg0'], check=True, stdout=subprocess.PIPE).stdout
        run(['wg', 'syncconf', 'wg0', '/dev/stdin'], input=body, check=True)
        say('адреса wg0 прежние — wg syncconf без рестарта')
        return False
    run(['systemctl', 'restart', 'wg-quick@wg0'], check=True)
    say('wg-quick@wg0 перезапущен')
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
    """Входы openvpn-server@server: их отпечаток решает, перезапускать ли службу.

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
