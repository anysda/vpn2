"""Шаги VPN-серверов entry для ролей wireguard, openvpn и ikev2."""
import os
import re
import subprocess
from pathlib import Path

from vpn2 import pki, wg

WG_DIR = '/etc/wireguard'
WG_ULA = 'fd66:66::1'
WG_NOTE = 'Initial config — panel will overwrite via syncWireguardConfig().'
# Приватный ключ WG - 32 байта в base64.
WG_PRIVATE = r'[A-Za-z0-9+/]{43}='


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
