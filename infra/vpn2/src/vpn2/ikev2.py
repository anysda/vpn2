"""Сервер IKEv2 на entry: режим сертификата, свой CA, конфиг swanctl, загрузка в charon.

letsencrypt: адрес профилей - домен панели, и Caddy выдал на него сертификат (или пара
восстановлена из бэкапа в swanctl). self-signed: иначе; свой ECDSA CA, в SAN адрес
профилей и IP входа. Панель читает режим и адрес из /etc/anysda/ikev2-mode и ikev2-server-host.

Сервер со своим CA и доменом отдаёт обе цепочки: профили, выданные до Let's Encrypt,
доверяют только своему CA. charon берёт сертификат, чей CA клиент назвал в CERTREQ, иначе
первый. Профилю по IP отвечает отдельная conn: её id совпадает с IDr клиента точнее %any.
"""
import glob
import os
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from vpn2 import pki
from vpn2.fsutil import lock

ETC = '/etc/anysda'
SWANCTL = '/etc/swanctl'
PKI = '/etc/strongswan/pki'
LOCK = '/run/anysda-ikev2-cert.lock'
LE_SYNC = '/usr/local/sbin/anysda-ikev2-le-sync.sh'
CADDY = '/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory'
UNIT = 'strongswan-starter'
SUBNET6 = 'fd68:68::/112'
# Только то, что charon читает при старте: остальное swanctl грузит без рестарта.
STARTER_INPUTS = ('/usr/lib/ipsec/charon', '/usr/lib/ipsec/starter', '/usr/lib/ipsec/plugins/*.so',
                  '/usr/lib/systemd/system/strongswan-starter.service', '/etc/strongswan.conf',
                  '/etc/strongswan.d/*.conf', '/etc/strongswan.d/charon/*.conf')
CONF = '''# Пишет стадия 27-ikev2. Секреты, conn и пул устройств - conf.d/anysda-clients.conf панели.
connections {{
  anysda-ikev2 {{
    version       = 2
    # Порядок = приоритет. Windows по умолчанию предлагает только MODP_1024: он последним.
    proposals     = aes256gcm16-prfsha384-ecp384,aes256gcm16-prfsha384-modp2048,aes256gcm16-prfsha256-ecp256,aes256-sha256-ecp256,aes256-sha256-modp2048,aes256gcm16-prfsha384-modp1024,aes256-sha256-modp1024,aes128-sha256-modp1024
    dpd_delay     = 30s
    pools         = anysda-ikev2-pool, anysda-ikev2-pool6
    fragmentation = yes
    encap         = yes
    rekey_time    = 0s

    local-server {{
      auth  = pubkey
      certs = {certs}
      id    = {local_id}
    }}
    remote-client {{
      auth   = eap-mschapv2
      eap_id = %any
    }}
    children {{
      net {{
        local_ts      = 0.0.0.0/0, ::/0
        # Нативные клиенты не делают PFS в CHILD_SA; Windows просит ESN.
        esp_proposals = aes256gcm16-ecp384-esn-noesn,aes256gcm16-esn-noesn,aes128gcm16-esn-noesn,aes256-sha256-esn-noesn,aes256-sha1-esn-noesn,aes128-sha256-esn-noesn,aes128-sha1-esn-noesn
        rekey_time    = 0s
        # DF во внешний ESP не копируем: на пути с малым MTU пакет с DF молча теряется.
        copy_df       = no
        if_id_in  = 42
        if_id_out = 42
      }}
    }}
  }}
{ip_conn}}}

pools {{
  anysda-ikev2-pool {{
    addrs = {subnet}
    dns   = {dns}
  }}
  anysda-ikev2-pool6 {{
    addrs = {subnet6}
  }}
}}
'''
IP_CONN = '''  anysda-ikev2-ip : connections.anysda-ikev2 {{
    local-server {{
      certs = anysda-own.crt
      id    = {entry}
    }}
  }}
'''
SERVER_PAIR = (('server.crt', 'x509/anysda-server.crt', 0o644), ('server.key', 'private/anysda-server.key', 0o600))
OWN_PAIR = (('server.crt', 'x509/anysda-own.crt', 0o644), ('server.key', 'private/anysda-own.key', 0o600))


class Plan(NamedTuple):
    mode: str
    server_host: str
    le_domain: str
    le_cert: str
    le_key: str
    local_id: str
    both: bool = False


def _read(path):
    try:
        return Path(path).read_text(errors='replace')
    except OSError:
        return ''


def _filled(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _readable(path):
    return bool(path) and os.access(path, os.R_OK)


def plan(entry_host, public_host, panel_domain, *, etc=ETC, swanctl=SWANCTL, caddy=CADDY):
    """Режим сертификата сервера по настройкам и состоянию узла."""
    mode, host = _read(f'{etc}/ikev2-mode'), _read(f'{etc}/ikev2-server-host')
    le_domain = panel_domain if panel_domain and public_host == panel_domain else ''
    le_cert = f'{caddy}/{le_domain}/{le_domain}.crt' if le_domain else ''
    le_key = f'{caddy}/{le_domain}/{le_domain}.key' if le_domain else ''
    # Restore возвращает пару LE в swanctl раньше, чем Caddy на чистом узле её получит.
    restored = (le_domain and mode == 'letsencrypt' and host == le_domain
                and _filled(f'{swanctl}/x509/anysda-server.crt') and _filled(f'{swanctl}/private/anysda-server.key'))
    if (_readable(le_cert) and _readable(le_key)) or restored:
        both = os.path.exists(f'{swanctl}/x509ca/anysda-ca.crt')
        return Plan('letsencrypt', le_domain, le_domain, le_cert, le_key, '%any' if both else le_domain, both)
    # id берётся из IDr клиента (старые профили - IP, новые - домен), оба есть в SAN.
    local_id = '%any' if public_host != entry_host else public_host
    return Plan('self-signed', public_host, le_domain, le_cert, le_key, local_id, bool(le_domain))


def conf_text(local_id, subnet, dns, own_id=''):
    """Базовая conn и пулы; own_id - IP входа, когда рядом отдаётся пара своего CA."""
    return CONF.format(local_id=local_id, subnet=subnet, subnet6=SUBNET6, dns=dns,
                       certs='anysda-server.crt, anysda-own.crt' if own_id else 'anysda-server.crt',
                       ip_conn=IP_CONN.format(entry=own_id) if own_id else '')


def _put(path, body, mode):
    """Файл с содержимым body и правами mode; True - что-то сменилось."""
    path = Path(path)
    try:
        if path.read_bytes() == body and path.stat().st_mode & 0o777 == mode:
            return False
    except OSError:
        pass
    path.write_bytes(body)
    path.chmod(mode)
    return True


def _creds(swanctl):
    return {str(path): path.read_bytes() for name in ('x509ca', 'x509', 'private')
            for path in sorted(Path(swanctl, name).glob('*')) if path.is_file()}


def own_certs(public_host, entry_host, say, *, target=PKI, swanctl=SWANCTL, run=pki.run, pairs=SERVER_PAIR):
    """Свой CA и сертификат сервера одной подменой каталога, затем выкладка в swanctl."""
    with pki.transaction(target) as work:
        say(f'[3/8] self-signed server cert (CN={public_host})')
        pki.build_ikev2(work, public_host, entry_host, say=say, run=run)
    for name, dest, mode in (('ca.crt', 'x509ca/anysda-ca.crt', 0o644), *pairs):
        _put(Path(swanctl, dest), Path(target, name).read_bytes(), mode)


def starter_inputs():
    return [path for pattern in STARTER_INPUTS for path in (sorted(glob.glob(pattern)) or [pattern])]


def server(entry_host, public_host, panel_domain, subnet, dns, say, restart, *,
           etc=ETC, swanctl=SWANCTL, target=PKI, caddy=CADDY, lock_path=LOCK, le_sync=LE_SYNC,
           run=subprocess.run, pki_run=pki.run, sleep=time.sleep):
    """Сертификаты, конфиг и загрузка в charon под замком применения сертификата.

    restart() перезапускает strongswan-starter по отпечатку входов (True при рестарте).
    Отметки режима пишутся после успешной загрузки. True - узел изменён.
    """
    for path in (target, f'{swanctl}/conf.d', etc):
        Path(path).mkdir(parents=True, exist_ok=True)
    for name in ('x509ca', 'x509', 'private'):
        Path(swanctl, name).mkdir(mode=0o700, exist_ok=True)
        Path(swanctl, name).chmod(0o700)
    with lock(lock_path):
        chosen = plan(entry_host, public_host, panel_domain, etc=etc, swanctl=swanctl, caddy=caddy)
        if chosen.mode == 'self-signed' and chosen.le_domain:
            say(f'PANEL_DOMAIN={chosen.le_domain} задан, но Caddy ещё не выдал LE-cert')
            say('  → временно self-signed; сертификат Caddy применится автоматически (проверка каждые 30 с)')
        say(f'mode={chosen.mode} server-host={chosen.server_host}')
        before = _creds(swanctl)
        stale = []
        if chosen.mode == 'letsencrypt':
            say(f'[2/8] LE-cert от Caddy ({chosen.le_domain})')
            if chosen.both:
                say('  свой CA остаётся рядом: профили, выданные до Let\'s Encrypt, продолжают работать')
                own_certs(public_host, entry_host, say, target=target, swanctl=swanctl, run=pki_run, pairs=OWN_PAIR)
            if _readable(chosen.le_cert) and _readable(chosen.le_key):
                run([le_sync, chosen.le_cert, chosen.le_key], check=True)
            else:
                say('  сохраняю восстановленные LE-cert/key/chain из swanctl до выдачи Caddy')
        else:
            say('[2/8] self-signed CA')
            own_certs(public_host, entry_host, say, target=target, swanctl=swanctl, run=pki_run,
                      pairs=SERVER_PAIR + OWN_PAIR if chosen.both else SERVER_PAIR)
            stale += sorted(Path(swanctl, 'x509ca').glob('anysda-le-chain-*.crt'))
        if not chosen.both:
            stale += [Path(swanctl, dest) for _name, dest, _mode in OWN_PAIR]
        dropped = [path for path in stale if path.exists()]
        for path in dropped:
            path.unlink()
        changed = _creds(swanctl) != before
        say('[4/8] swanctl conf')
        text = conf_text(chosen.local_id, subnet, dns, entry_host if chosen.both else '')
        changed |= _put(Path(swanctl, 'conf.d/anysda.conf'), text.encode(), 0o644)
        say('[6/8] strongswan-starter service')
        restarted = restart()
        # Готов - когда charon отвечает по vici.
        for _ in range(10 if restarted else 0):
            if run(['swanctl', '--stats'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            sleep(0.2)
        # Убранное с диска charon держит в памяти, пока загрузка не начата с чистого набора.
        run(['swanctl', '--load-creds', *(['--clear'] if dropped else [])], check=True, stdout=subprocess.DEVNULL)
        for what in ('--load-pools', '--load-conns'):
            run(['swanctl', what], check=True, stdout=subprocess.DEVNULL)
        for name, value in (('ikev2-server-host', chosen.server_host), ('ikev2-mode', chosen.mode),
                            ('ikev2-domain', chosen.le_domain)):
            changed |= _put(Path(etc, name), value.encode(), 0o644)
        for name in ('ikev2-profile-notice', 'ikev2-own-ca-kept'):
            Path(etc, name).unlink(missing_ok=True)
        say(f'27-ikev2 — mode={chosen.mode} server-host={chosen.server_host}')
    return changed or restarted
