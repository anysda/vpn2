"""Шаги роли adguard: бинарник, учётная запись и конфиги, перезапуск по отпечатку.

Вызываются как `vpn2 adguard <шаг>`; каждый печатает changed=0|1 последней
строкой. Переменные стадии приходят окружением процесса. Шаги config и restart
зовёт ещё и восстановление из бэкапа (infra/scripts/22-adguard.sh).
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import string
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import yaml

from vpn2 import fsutil, release, svc
from vpn2.adguard import dns

AGH_VER = 'v0.107.80'
# GitHub release assets API, поле digest архива AdGuardHome_linux_amd64.tar.gz.
AGH_SHA256 = '28593bc761d71469abdbc6f39ca42cbed763ec92b63082c729177b037ed51d18'
AGH_URL = f'https://github.com/AdguardTeam/AdGuardHome/releases/download/{AGH_VER}/AdGuardHome_linux_amd64.tar.gz'
AGH_DIR = '/opt/AdGuardHome'
PLAIN_DIR = '/opt/AdGuardHome-unfiltered'
ETC = '/etc/anysda'
UNITS = {'adguardhome': AGH_DIR, 'adguardhome-unfiltered': PLAIN_DIR}
UNIT_DIR = '/etc/systemd/system'
MANIFEST = '/tmp/anysda/release-manifest.json'
RESOURCES = '/tmp/anysda/entry_resources.py'
POOLS = '/tmp/anysda/vpn-pools.json'
POOL_KINDS = ('wg', 'ovpn', 'ikev2')


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
            body = tar.extractfile(member).read()
        fsutil.atomic(binary, body, mode=0o755)
    print(f'AdGuard Home {installed_version(binary)} установлен')
    return True


def agh_password(password):
    """bcrypt ограничен 72 байтами: длинный пароль панели преобразуется целиком, без усечения.

    Этот же пароль передаёт AdGuard прокси панели (стадия 30).
    """
    raw = password.encode()
    return password if len(raw) <= 72 else hashlib.sha256(b'vpn2-adguard-v1\0' + raw).hexdigest()


def _write_secret(path, text):
    path = Path(path)
    if path.is_file() and path.read_text() == text and path.stat().st_mode & 0o777 == 0o600:
        return False
    fsutil.atomic(path, text, mode=0o600)
    return True


def credentials(environ, etc=ETC):
    """Единый логин панели: (пользователь, пароль для AdGuard, сменились ли файлы).

    Пустой ADMIN_PASSWORD - пароль создаётся один раз в admin-password.txt,
    его же потом берёт стадия 30.
    """
    admin = Path(etc, 'admin-password.txt')
    changed = False
    if environ.get('ADMIN_PASSWORD'):
        changed = _write_secret(admin, environ['ADMIN_PASSWORD'])
    elif not admin.is_file():
        alphabet = string.ascii_letters + string.digits
        changed = _write_secret(admin, ''.join(secrets.choice(alphabet) for _ in range(18)))
    password = agh_password(admin.read_text())
    changed |= _write_secret(Path(etc, 'adguard-password.txt'), password)
    return environ.get('ADMIN_USER') or 'anysda', password, changed


def hash_matches(user, password, password_hash):
    with tempfile.NamedTemporaryFile('w', suffix='.htpasswd') as handle:
        handle.write(f'{user}:{password_hash}\n')
        handle.flush()
        return subprocess.run(['htpasswd', '-vb', handle.name, user, password],
                              capture_output=True, check=False).returncode == 0


def new_hash(password):
    out = subprocess.run(['htpasswd', '-bnBC', '10', '', password], capture_output=True, text=True,
                         check=True).stdout
    return out.replace(':', '').replace('\n', '').replace('$2y', '$2a', 1)


def password_hash(cfg, user, password, matches=hash_matches, create=new_hash):
    """Соль bcrypt случайная: хэш из конфига остаётся, если подходит к паролю, иначе AdGuard
    перезапускался бы на каждом прогоне."""
    current = dns.user_password(cfg, user)
    if current and matches(user, password, current):
        return current
    return create(password)


def pool_subnets(environ, pools_file):
    pools = json.loads(Path(pools_file).read_text())
    subnets = [net for kind in POOL_KINDS for net in pools[kind]['subnets']]
    return [*subnets,
            (environ.get('WG_SERVER_IP') or '10.66.66.1') + '/24',
            (environ.get('OVPN_SUBNET') or '10.67.67.0') + '/24',
            environ.get('IKEV2_SUBNET') or '10.68.68.0/24']


def resource_tool(environ, *args):
    tool = environ.get('VPN2_ENTRY_RESOURCES') or RESOURCES
    env = {**os.environ, 'ENTRY_RESOURCE_OVERRIDES': environ.get('ENTRY_RESOURCE_OVERRIDES', '')}
    return subprocess.run([sys.executable, tool, *args], capture_output=True, text=True, check=True,
                          env=env).stdout


def limits(environ, *machine):
    """Лимиты памяти и профиль журнала AdGuard по ресурсам entry: словарь AGH_*.

    machine - размер машины аргументами entry_resources вместо замера на месте.
    """
    found = {}
    for line in resource_tool(environ, *machine, '--service', 'agh').splitlines():
        name, _, value = line.partition('=')
        if name.startswith('AGH_'):
            found[name] = shlex.split(value)[0]
    return found


def _load(path):
    try:
        return yaml.safe_load(Path(path).read_text())
    except FileNotFoundError:
        return None


def _dump(cfg):
    return yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _state(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def resource_patch(environ, staged, fresh, state):
    """Журнал запросов и список AdAway по ресурсам entry: поля ведёт entry_resources."""
    resource_tool(environ, '--agh-config', str(staged), *(['--fresh'] if fresh else []),
                  '--agh-state', str(state))


def build_configs(environ, user, password, *, agh_dir=AGH_DIR, plain_dir=PLAIN_DIR, etc=ETC,
                  pools_file=POOLS, work=None, hasher=password_hash, profile=None, patch=None):
    """Готовит оба конфига в work и возвращает пути (основной, без фильтров).

    Живой конфиг не перезаписывается целиком: его правят в панели и сам AdGuard.
    Текст меняется только при смысловой разнице, формат YAML AdGuard ведёт сам.
    """
    port = environ['AGH_PORT']
    bind = environ.get('MGMT_IP') or '10.99.0.1'
    live, plain_live = Path(agh_dir, 'AdGuardHome.yaml'), Path(plain_dir, 'AdGuardHome.yaml')
    staged, plain_staged = Path(work, 'AdGuardHome.yaml'), Path(work, 'AdGuardHome-unfiltered.yaml')
    cfg = _load(live)
    digest = hasher(cfg, user, password)
    fresh = cfg is None
    if fresh:
        found = (profile or limits)(environ)
        text = dns.fresh_text(user, digest, port, bind, found['AGH_ADAWAY'], found['AGH_QUERYLOG'])
        cfg = yaml.safe_load(text)
    else:
        text = live.read_text()
        if dns.sync_credentials(cfg, user, digest):
            text = _dump(cfg)
            print('учётная запись и ECS приведены к настройкам')
    before = yaml.safe_load(text)
    state_path = Path(etc, 'agh-dns.json')
    state = _state(state_path)
    written, kept, added = dns.patch_policy(cfg, state, pool_subnets(environ, pools_file), bind, port)
    if written != state or not state_path.exists():
        fsutil.atomic(state_path, json.dumps(written, ensure_ascii=False, sort_keys=True) + '\n', mode=0o600)
    if kept:
        sys.stderr.write(f'настройки из панели оставлены: {", ".join(kept)}\n')
    if cfg != before:
        text = _dump(cfg)
        print(f'политика DNS приведена к настройкам; ratelimit_whitelist += {added}')
    fsutil.atomic(staged, text, mode=0o600)
    (patch or resource_patch)(environ, staged, fresh, Path(etc, 'agh-resources.json'))
    filtered = _load(staged)
    plain_before = _load(plain_live)
    plain_text = plain_live.read_text() if plain_before is not None else ''
    plain_after = dns.plain_config(filtered, plain_before or {}, port)
    if plain_after != (plain_before or {}):
        plain_text = yaml.safe_dump(plain_after, allow_unicode=True, sort_keys=False)
    fsutil.atomic(plain_staged, plain_text, mode=0o600)
    return staged, plain_staged


def publish(unit, staged, target, *, active=svc.active, stop=None, forget=svc.forget):
    """AdGuard сохраняет YAML при остановке: останавливается только изменившийся экземпляр."""
    staged, target = Path(staged), Path(target)
    if target.is_file() and target.read_bytes() == staged.read_bytes():
        return False
    if active(unit):
        (stop or (lambda name: subprocess.run(['systemctl', 'stop', name], check=True)))(unit)
    target.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic(target, staged.read_bytes(), mode=0o600)
    forget(unit)
    return True


def configure(environ):
    if not environ.get('AGH_PORT'):
        raise StageError('AGH_PORT не задан')
    Path(ETC).mkdir(parents=True, exist_ok=True)
    for directory in UNITS.values():
        Path(directory).mkdir(parents=True, exist_ok=True)
    user, password, changed = credentials(environ)
    print(f'{user} / (пароль в {ETC}/adguard-password.txt)')
    pools = environ.get('VPN2_POOLS') or POOLS
    with tempfile.TemporaryDirectory() as work:
        staged = build_configs(environ, user, password, pools_file=pools, work=work)
        for (unit, directory), path in zip(UNITS.items(), staged):
            changed |= publish(unit, path, Path(directory, 'AdGuardHome.yaml'))
    return changed


def listening():
    out = subprocess.run(['ss', '-Hlunp', 'sport = :53'], capture_output=True, text=True, check=False).stdout
    return '"AdGuardHome"' in out


def restart():
    binary = Path(AGH_DIR, 'AdGuardHome')
    changed = False
    for unit in UNITS:
        changed |= svc.restart_if_changed(unit, [binary, Path(UNIT_DIR, f'{unit}.service')])
    if changed:
        for _ in range(10):
            if listening():
                break
            time.sleep(0.2)
    return changed


def main(argv):
    parser = argparse.ArgumentParser(prog='vpn2 adguard')
    parser.add_argument('step', choices=('binary', 'limits', 'config', 'restart'))
    args = parser.parse_args(argv)
    environ = os.environ
    try:
        if args.step == 'limits':
            print(json.dumps(limits(environ), sort_keys=True))
            return 0
        if args.step == 'binary':
            changed = install_binary(environ)
        elif args.step == 'config':
            changed = configure(environ)
        else:
            changed = restart()
    except (StageError, KeyError, OSError, subprocess.CalledProcessError) as error:
        sys.stderr.write(f'vpn2 adguard {args.step}: {error!r}\n')
        return 1
    print(f'changed={int(changed)}')
    return 0
