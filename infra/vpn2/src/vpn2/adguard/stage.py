"""Шаги роли adguard: `vpn2 adguard binary|limits|config|restart`.

Шаг печатает changed=0|1 последней строкой; config и restart зовёт и восстановление из бэкапа.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import string
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import yaml

from vpn2 import fsutil, release, resources, svc
from vpn2.adguard import dns

AGH_VER = 'v0.107.80'
# GitHub release assets API, поле digest архива AdGuardHome_linux_amd64.tar.gz.
AGH_SHA256 = '28593bc761d71469abdbc6f39ca42cbed763ec92b63082c729177b037ed51d18'
AGH_URL = f'https://github.com/AdguardTeam/AdGuardHome/releases/download/{AGH_VER}/AdGuardHome_linux_amd64.tar.gz'
AGH_DIR = '/opt/AdGuardHome'
PLAIN_DIR = '/opt/AdGuardHome-unfiltered'
ETC = '/etc/anysda'
UNITS = {'adguardhome': AGH_DIR, 'adguardhome-unfiltered': PLAIN_DIR}
MANIFEST = '/tmp/anysda/release-manifest.json'
POOLS = '/tmp/anysda/vpn-pools.json'


class StageError(Exception):
    pass


def installed_version(binary):
    try:
        out = subprocess.run([str(binary), '--version'], capture_output=True, text=True, check=False).stdout
    except OSError:
        return ''
    found = re.search(r'v[\d.]+', out)
    return found.group(0) if found else ''


def install_binary(environ, directory=AGH_DIR):
    """Архив сверяется по SHA-256 до распаковки: из манифеста выпуска, в разработке - AGH_SHA256."""
    binary = Path(directory, 'AdGuardHome')
    if os.access(binary, os.X_OK) and installed_version(binary) == AGH_VER:
        print(f'AdGuard Home {AGH_VER} уже установлен')
        return False
    Path(directory).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        archive = os.path.join(work, 'AdGuardHome.tar.gz')
        argv = ['artifact', 'adguard', archive, '--url', AGH_URL, '--sha256', AGH_SHA256]
        if environ.get('VPN2_DEVELOPMENT', 'false') != 'true':
            argv = ['--manifest', environ.get('VPN2_RELEASE_MANIFEST') or MANIFEST, *argv]
        if release.main(argv):
            raise StageError('архив AdGuard Home не получен')
        with tarfile.open(archive) as tar:
            member = next((item for item in tar.getmembers() if item.isfile()
                           and item.name.removeprefix('./') == 'AdGuardHome/AdGuardHome'), None)
            if member is None:
                raise StageError('в архиве AdGuard Home нет бинарника')
            fsutil.atomic(binary, tar.extractfile(member).read(), mode=0o755)
    print(f'AdGuard Home {installed_version(binary)} установлен')
    return True


def agh_password(password):
    """bcrypt берёт 72 байта: длинный пароль панели преобразуется целиком; его же передаёт панель."""
    raw = password.encode()
    return password if len(raw) <= 72 else hashlib.sha256(b'vpn2-adguard-v1\0' + raw).hexdigest()


def random_text(length):
    return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))


def admin_password(environ, etc=ETC):
    """Пароль админа: из config.yaml, иначе прежний, иначе новый. Вернуть (пароль, сменился ли файл)."""
    path = Path(etc, 'admin-password.txt')
    changed = False
    if environ.get('ADMIN_PASSWORD'):
        changed = fsutil.publish(path, environ['ADMIN_PASSWORD'])
    elif not path.is_file():
        changed = fsutil.publish(path, random_text(18))
    os.chmod(path, 0o600)
    return path.read_text(), changed


def password_hash(cfg, user, password):
    """Хэш из конфига остаётся, пока подходит к паролю: соль bcrypt случайная."""
    current = dns.user_password(cfg, user)
    if current:
        with tempfile.NamedTemporaryFile('w', suffix='.htpasswd') as handle:
            handle.write(f'{user}:{current}\n')
            handle.flush()
            if subprocess.run(['htpasswd', '-vb', handle.name, user, password], capture_output=True,
                              check=False).returncode == 0:
                return current
    out = subprocess.run(['htpasswd', '-bnBC', '10', '', password], capture_output=True, text=True,
                         check=True).stdout
    return out.replace(':', '').replace('\n', '').replace('$2y', '$2a', 1)


def pool_subnets(environ, pools_file):
    pools = json.loads(Path(pools_file).read_text())
    return [*(net for kind in ('wg', 'ovpn', 'ikev2') for net in pools[kind]['subnets']),
            (environ.get('WG_SERVER_IP') or '10.66.66.1') + '/24',
            (environ.get('OVPN_SUBNET') or '10.67.67.0') + '/24',
            environ.get('IKEV2_SUBNET') or '10.68.68.0/24']


def _load(path):
    try:
        return yaml.safe_load(Path(path).read_text())
    except FileNotFoundError:
        return None


def _json(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def _dump(cfg):
    return yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)


def build_configs(environ, user, password, work, *, agh_dir=AGH_DIR, plain_dir=PLAIN_DIR, etc=ETC, pools=POOLS):
    """Оба конфига в work: (основной, без фильтров). Живой конфиг правят панель и сам AdGuard,
    поэтому текст меняется только при смысловой разнице."""
    port, bind = environ['AGH_PORT'], environ.get('MGMT_IP') or '10.99.0.1'
    live, plain_live = Path(agh_dir, 'AdGuardHome.yaml'), Path(plain_dir, 'AdGuardHome.yaml')
    values = resources.entry(environ)
    cfg = _load(live)
    digest = password_hash(cfg, user, password)
    fresh = cfg is None
    if fresh:
        profile = resources.adguard(values)
        text = dns.fresh_text(user, digest, port, bind, profile['AGH_ADAWAY'], profile['AGH_QUERYLOG'])
        cfg = yaml.safe_load(text)
    else:
        text = live.read_text()
        if dns.sync_credentials(cfg, user, digest):
            text = _dump(cfg)
            print('учётная запись и ECS приведены к настройкам')
    before = yaml.safe_load(text)
    state_path = Path(etc, 'agh-dns.json')
    state = _json(state_path)
    written, kept, added = dns.patch_policy(cfg, state, pool_subnets(environ, pools), bind, port)
    if written != state or not state_path.exists():
        fsutil.atomic(state_path, json.dumps(written, ensure_ascii=False, sort_keys=True) + '\n')
    if kept:
        sys.stderr.write(f'настройки из панели оставлены: {", ".join(kept)}\n')
    if cfg != before:
        print(f'политика DNS приведена к настройкам; ratelimit_whitelist += {added}')
    overrides = json.loads(environ.get('ENTRY_RESOURCE_OVERRIDES') or '{}')
    managed_path = Path(etc, 'agh-resources.json')
    managed = resources.patch_adguard(cfg, _json(managed_path), values, overrides, fresh)
    if managed != _json(managed_path) or not managed_path.exists():
        fsutil.atomic(managed_path, json.dumps(managed, sort_keys=True) + '\n')
    if cfg != before:
        text = _dump(cfg)
    staged, plain_staged = Path(work, 'AdGuardHome.yaml'), Path(work, 'AdGuardHome-unfiltered.yaml')
    fsutil.atomic(staged, text)
    plain_before = _load(plain_live)
    plain_after = dns.plain_config(cfg, plain_before or {}, port)
    plain_text = plain_live.read_text() if plain_before is not None else ''
    if plain_after != (plain_before or {}):
        plain_text = yaml.safe_dump(plain_after, allow_unicode=True, sort_keys=False)
    fsutil.atomic(plain_staged, plain_text)
    return staged, plain_staged


def publish(unit, staged, target):
    """AdGuard сохраняет YAML при остановке: останавливается только изменившийся экземпляр."""
    if target.is_file() and target.read_bytes() == staged.read_bytes():
        return False
    if svc.active(unit):
        subprocess.run(['systemctl', 'stop', unit], check=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic(target, staged.read_bytes())
    svc.forget(unit)
    return True


def configure(environ):
    if not environ.get('AGH_PORT'):
        raise StageError('AGH_PORT не задан')
    for directory in (ETC, *UNITS.values()):
        Path(directory).mkdir(parents=True, exist_ok=True)
    password, changed = admin_password(environ, ETC)
    password = agh_password(password)
    changed |= fsutil.publish(Path(ETC, 'adguard-password.txt'), password)
    user = environ.get('ADMIN_USER') or 'anysda'
    print(f'{user} / (пароль в {ETC}/adguard-password.txt)')
    with tempfile.TemporaryDirectory() as work:
        staged = build_configs(environ, user, password, work, pools=environ.get('VPN2_POOLS') or POOLS)
        for (unit, directory), path in zip(UNITS.items(), staged):
            changed |= publish(unit, path, Path(directory, 'AdGuardHome.yaml'))
    return changed


def restart():
    changed = False
    for unit in UNITS:
        changed |= svc.restart_if_changed(unit, [Path(AGH_DIR, 'AdGuardHome'), f'/etc/systemd/system/{unit}.service'])
    for _ in range(10 if changed else 0):
        listening = subprocess.run(['ss', '-Hlunp', 'sport = :53'], capture_output=True, text=True, check=False)
        if '"AdGuardHome"' in listening.stdout:
            break
        time.sleep(0.2)
    return changed


def main(argv):
    parser = argparse.ArgumentParser(prog='vpn2 adguard')
    parser.add_argument('step', choices=('binary', 'limits', 'config', 'restart'))
    step = parser.parse_args(argv).step
    try:
        if step == 'limits':
            print(json.dumps(resources.adguard(resources.entry(os.environ)), sort_keys=True))
            return 0
        steps = {'binary': lambda: install_binary(os.environ), 'config': lambda: configure(os.environ),
                 'restart': restart}
        changed = steps[step]()
    except (StageError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        sys.stderr.write(f'vpn2 adguard {step}: {error!r}\n')
        return 1
    print(f'changed={int(changed)}')
    return 0
