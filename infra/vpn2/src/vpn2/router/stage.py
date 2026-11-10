"""vpn2 router rule-sets|base-config|restart|ssh-config|legacy - шаги роли router.

Каждый шаг печатает последней строкой `changed=0|1`: по ней роль решает,
менялся ли узел.
"""

import argparse
import hashlib
import io
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

from vpn2 import fsutil
from vpn2.router import config

SING_BOX = '/usr/local/bin/sing-box'
RULE_SET_DIR = '/var/lib/sing-box/rule-set'
RULE_SET_MAX_AGE = 7 * 86400
CLASH_SECRET = '/etc/anysda/clash-secret.txt'
EXIT_CERTS = '/etc/sing-box/exit-certs'
BASE_CONFIG = '/etc/sing-box/config-base.json'
FINGERPRINT = '/var/lib/sing-box/applied.sha256'
# Что читает работающий sing-box: смена любого файла - повод для перезапуска.
FINGERPRINT_INPUTS = (SING_BOX, '/etc/systemd/system/sing-box.service', '/etc/sing-box/config.json')
SSH_DIR = '/root/.ssh'
# Пароль экзита в алфавите его генератора; остальное в файле секретов - чужое.
SECRET_LINE = re.compile(r'([A-Z][A-Z0-9_]*_(?:DIRECT|WARP|OBFS|MGMT|CLASH))=([A-Za-z0-9+/]{16,128})')
SECRET_ALPHABET = string.ascii_letters + string.digits
SECRET_KINDS = ('DIRECT', 'WARP', 'MGMT', 'OBFS')
REALITY_KEYS = {'PUB': 'REALITY_PUBKEY', 'SID': 'REALITY_SHORT_ID', 'SNI': 'REALITY_SNI',
                'PORT': 'REALITY_PORT'}


class StageError(Exception):
    """Шаг не выполнен; текст уходит оператору."""


def upper(tag):
    return tag.upper().replace('-', '_')


def rule_set_fresh(path, check, now):
    return path.is_file() and now - path.stat().st_mtime <= RULE_SET_MAX_AGE and check(path)


def sync_rule_sets(directory, names, fetch, check, now=None):
    """Качает устаревшие и битые наборы; файл меняется только проверенным."""
    now = time.time() if now is None else now
    directory.mkdir(parents=True, exist_ok=True)
    changed = False
    for name in names:
        path = directory / f'{name}.srs'
        if rule_set_fresh(path, check, now):
            continue
        fresh = directory / f'{name}.srs.new'
        fetch(name, fresh)
        # HTTP 200 не гарантирует SRS: проверка идёт до замены, пока sing-box
        # читает прежний набор.
        if not check(fresh):
            fresh.unlink(missing_ok=True)
            raise StageError(f'набор {name} повреждён - сохраняю предыдущий файл')
        os.replace(fresh, path)
        changed = True
    for stale in ('geoip.db', 'geosite.db'):
        old = directory.parent / stale
        if old.exists():
            old.unlink()
            changed = True
    return changed


def fetch_rule_set(name, dest):
    url = f'https://raw.githubusercontent.com/SagerNet/sing-{name.split("-")[0]}/rule-set/{name}.srs'
    subprocess.run(['curl', '-sSfL', '--retry', '5', '--retry-delay', '3', '--retry-all-errors',
                    '--connect-timeout', '20', '-o', str(dest), url], check=True)


def check_rule_set(path):
    return subprocess.run([SING_BOX, 'rule-set', 'decompile', '--output', '/dev/null', str(path)],
                          capture_output=True).returncode == 0


def parse_secrets(text, source='foreign-secrets.env'):
    """Пароли экзитов; файл не исполняется, чужая строка обрывает шаг."""
    found = {}
    for line in text.splitlines():
        if not line:
            continue
        match = SECRET_LINE.fullmatch(line)
        if not match:
            raise StageError(f'в {source} чужая строка ({line.split("=")[0]}) - '
                             'экзит отдал не пароль, стадию прерываю')
        found[match.group(1)] = match.group(2)
    return found


def clash_secret(path):
    """Секрет clash-api создаётся один раз и переживает передеплой."""
    path = Path(path)
    value = path.read_text().strip() if path.is_file() else ''
    if not value:
        value = ''.join(secrets.choice(SECRET_ALPHABET) for _ in range(32))
        fsutil.atomic(path, value, mode=0o600)
        return value, True
    if path.stat().st_mode & 0o777 != 0o600:
        path.chmod(0o600)
        return value, True
    return value, False


def install_file(src, dest):
    """Копия с правами 0600; True, если содержимое или права сменились."""
    body = Path(src).read_bytes()
    dest = Path(dest)
    if dest.is_file() and dest.read_bytes() == body and dest.stat().st_mode & 0o777 == 0o600:
        return False
    fsutil.atomic(dest, body, mode=0o600)
    return True


def exit_environment(environ, foreign, certs_src, certs_dir, wan_iface):
    """Окружение генератора конфига: секреты, порты и закрепление экзитов.

    Возвращает окружение и признак того, что файлы закрепления сменились.
    """
    env = {key: value for key, value in environ.items() if value != ''}
    env['WG_OUT_IFACE'] = wan_iface
    for key, default in (('YT_ROUTE', 'off'), ('YT_QUIC', 'block'), ('YT_MARK', '256')):
        env.setdefault(key, default)
    certs_dir = Path(certs_dir)
    certs_dir.mkdir(parents=True, exist_ok=True)
    changed = certs_dir.stat().st_mode & 0o777 != 0o700
    certs_dir.chmod(0o700)
    for tag in env.get('EXIT_TAGS', '').split():
        T = upper(tag)
        for kind in SECRET_KINDS:
            if not foreign.get(f'{T}_{kind}'):
                raise StageError(f'секрет {tag} {kind} не найден в foreign-secrets.env')
            env[f'{T}_{kind}'] = foreign[f'{T}_{kind}']
        env.setdefault(f'{T}_HY2_DIRECT_PORT', env['HY2_DIRECT_PORT'])
        env.setdefault(f'{T}_HY2_MGMT_PORT', env['HY2_MGMT_PORT'])
        # Сертификат, не приехавший в этот раз, остаётся закреплённым с прошлого.
        for suffix in ('pem', 'reality'):
            src = Path(certs_src) / f'{tag}.{suffix}'
            if src.is_file():
                changed |= install_file(src, certs_dir / f'{tag}.{suffix}')
        pem = certs_dir / f'{tag}.pem'
        if pem.is_file():
            env[f'{T}_HY2_CERT_PATH'] = str(pem)
        # Открытая часть Reality экзита: строки читаются по одной, файл не исполняется.
        reality = certs_dir / f'{tag}.reality'
        if reality.is_file():
            for line in reality.read_text().splitlines():
                key, _, value = line.partition('=')
                if key in REALITY_KEYS:
                    env[f'{T}_{REALITY_KEYS[key]}'] = value
    return env, changed


def wan_iface():
    out = subprocess.run(['ip', '-4', '-o', 'route', 'show', 'default'],
                         capture_output=True, text=True, check=True).stdout.split()
    if 'dev' not in out:
        raise StageError('маршрута по умолчанию нет - WAN-интерфейс не определить')
    return out[out.index('dev') + 1]


def render_config(env):
    """Конфиг роутера текстом; генератор читает окружение процесса."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            config.main()
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return out.getvalue()


def publish_config(body, dest, check):
    """Проверенный конфиг встаёт на место атомарно; прежний при отказе цел."""
    dest = Path(dest)
    if dest.is_file() and dest.read_text() == body and dest.stat().st_mode & 0o777 == 0o600:
        return False
    candidate = dest.with_name(f'.{dest.name}.new')
    fsutil.atomic(candidate, body, mode=0o600)
    try:
        if not check(candidate):
            raise StageError('конфиг невалиден - сервис не трогаю')
        os.replace(candidate, dest)
    finally:
        candidate.unlink(missing_ok=True)
    return True


def check_config(path):
    return subprocess.run([SING_BOX, 'check', '-c', str(path)]).returncode == 0


def fingerprint(paths):
    digest = hashlib.sha256()
    for path in paths:
        try:
            digest.update(Path(path).read_bytes())
        except OSError:
            continue
    return digest.hexdigest()


def fingerprint_inputs():
    return [*FINGERPRINT_INPUTS, *sorted(Path(EXIT_CERTS).glob('*.pem')),
            '/etc/sysctl.d/99-anysda.conf']


def restart(fp_file=FINGERPRINT):
    """Перезапуск рвёт соединения клиентов, поэтому только при смене входов."""
    fp_file = Path(fp_file)
    current = fingerprint(fingerprint_inputs())
    active = subprocess.run(['systemctl', 'is-active', '--quiet', 'sing-box']).returncode == 0
    if active and fp_file.is_file() and fp_file.read_text().strip() == current:
        print('конфиг, сертификаты и бинарник прежние - sing-box не перезапускаю')
        return False
    subprocess.run(['systemctl', 'restart', 'sing-box'], check=True)
    fsutil.atomic(fp_file, current + '\n', mode=0o644)
    # verify идёт сразу после стадии и ходит в clash-api: ждём его порт.
    for _ in range(50):
        if subprocess.run(['ss', '-Hltn', 'sport = :9090'], capture_output=True, text=True).stdout.strip():
            break
        time.sleep(0.2)
    print('sing-box перезапущен')
    return True


def ssh_config_text(tags, environ, known_hosts):
    """Псевдонимы ssh <tag>: nc в локальный SOCKS-инбаунд mon-<tag> служебного туннеля."""
    lines = ['# anysda-vpn - генерирует vpn2 router ssh-config, руками не править']
    hosts = []
    for tag in tags:
        ip = environ.get(f'MGMT_IP_{upper(tag)}', '')
        if not ip:
            sys.stderr.write(f'ВНИМАНИЕ: MGMT_IP_{upper(tag)} не задан - ssh {tag} не настроен\n')
            continue
        hosts.append(tag)
        lines += ['', f'Host {tag}', '  HostName 127.0.0.1', '  Port 22', '  User root',
                  '  IdentityFile /root/.ssh/id_ed25519_mgmt', '  IdentitiesOnly yes',
                  f'  HostKeyAlias anysda-{tag}', f'  UserKnownHostsFile {known_hosts}',
                  '  StrictHostKeyChecking accept-new', '  ServerAliveInterval 15',
                  f'  ProxyCommand nc -X 5 -x 127.0.0.1:{10100 + int(ip.rsplit(".", 1)[-1])} %h %p']
    return '\n'.join(lines) + '\n', hosts


def known_hosts_text(tags, hostkeys):
    """Ключи экзитов под псевдонимом anysda-<tag>: адрес у всех 127.0.0.1."""
    lines = []
    for tag in tags:
        path = Path(hostkeys) / f'{tag}.pub'
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 2:
                lines.append(f'anysda-{tag} {fields[0]} {fields[1]}')
                break
    return ''.join(line + '\n' for line in lines)


def write_if_changed(path, body, mode=0o600):
    path = Path(path)
    if path.is_file() and path.read_text() == body and path.stat().st_mode & 0o777 == mode:
        return False
    fsutil.atomic(path, body, mode=mode)
    return True


def ssh_config(tags, environ, hostkeys, ssh_dir=SSH_DIR):
    ssh_dir = Path(ssh_dir)
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    ssh_dir.chmod(0o700)
    conf, known = ssh_dir / 'anysda-mgmt.conf', ssh_dir / 'known_hosts_mgmt'
    text, hosts = ssh_config_text(tags, environ, known)
    changed = write_if_changed(conf, text)
    # Собирается заново с ключей, снятых с экзитов: переустановленный экзит не
    # упрётся в старый ключ.
    changed |= write_if_changed(known, known_hosts_text(tags, hostkeys))
    main_conf = ssh_dir / 'config'
    current = main_conf.read_text() if main_conf.is_file() else ''
    include = f'Include {conf}'
    # Include обязан стоять до первого Host, иначе попадёт внутрь его блока.
    body = current if include in current.splitlines() else f'{include}\n{current}'
    changed |= write_if_changed(main_conf, body)
    print('хосты: ' + ' '.join(hosts))
    return changed


def legacy():
    """Следы Shadowsocks/Outline прежних выпусков; на чистом узле ничего не делает."""
    changed = False
    for unit in ('anysda-iptables', 'outline-ss-server'):
        path = Path(f'/etc/systemd/system/{unit}.service')
        if path.is_file():
            subprocess.run(['systemctl', 'disable', '--now', unit], capture_output=True)
            path.unlink()
            changed = True
    script = Path('/usr/local/sbin/anysda-iptables.sh')
    if os.access(script, os.X_OK):
        subprocess.run([str(script), 'down'], capture_output=True)
    for path in (script, Path('/usr/local/bin/outline-ss-server'), Path('/etc/outline-ss-server'),
                 Path('/usr/local/sbin/vpn2-entry-addrs'), Path('/usr/local/sbin/anysda-apply-routes.py')):
        if path.is_dir():
            shutil.rmtree(path)
            changed = True
        elif path.exists():
            path.unlink()
            changed = True
    if subprocess.run(['id', 'outline'], capture_output=True).returncode == 0:
        subprocess.run(['userdel', 'outline'], capture_output=True)
        changed = True
    # clash-api слушает адрес на lo: правило ufw времён mesh больше не нужно.
    rule = ['allow', 'proto', 'tcp', 'from', '10.99.0.0/24', 'to', 'any', 'port', '9090']
    if shutil.which('ufw'):
        out = subprocess.run(['ufw', 'delete', *rule], capture_output=True, text=True).stdout
        changed |= 'Rule deleted' in out
    return changed


def base_config(args):
    foreign_file = Path(args.secrets)
    if not foreign_file.is_file():
        raise StageError(f'{foreign_file} не найден - оркестратор должен был его загрузить')
    secret, changed = clash_secret(args.clash_secret)
    env, pinned = exit_environment(os.environ, parse_secrets(foreign_file.read_text(), foreign_file),
                                   args.certs, EXIT_CERTS, wan_iface())
    env.update(RU_CLASH_SECRET=secret, VPN_POOLS_FILE=args.pools)
    # База обновляется при каждом прогоне: устаревшая молча теряет инбаунды,
    # добавленные в генератор после первого деплоя.
    return publish_config(render_config(env), BASE_CONFIG, check_config) | changed | pinned


def main(argv):
    parser = argparse.ArgumentParser(prog='vpn2 router')
    sub = parser.add_subparsers(dest='step', required=True)
    sub.add_parser('rule-sets')
    step = sub.add_parser('base-config')
    step.add_argument('--secrets', default='/tmp/anysda/foreign-secrets.env')
    step.add_argument('--certs', default='/tmp/anysda/exit-certs')
    step.add_argument('--pools', default='/tmp/anysda/vpn-pools.json')
    step.add_argument('--clash-secret', default=CLASH_SECRET)
    sub.add_parser('restart')
    step = sub.add_parser('ssh-config')
    step.add_argument('--hostkeys', default='/tmp/anysda/exit-hostkeys')
    sub.add_parser('legacy')
    args = parser.parse_args(argv)
    try:
        if args.step == 'rule-sets':
            changed = sync_rule_sets(Path(RULE_SET_DIR), config.RULE_SETS, fetch_rule_set, check_rule_set)
        elif args.step == 'base-config':
            changed = base_config(args)
        elif args.step == 'restart':
            changed = restart()
        elif args.step == 'ssh-config':
            changed = ssh_config(os.environ.get('EXIT_TAGS', '').split(), os.environ, args.hostkeys)
        else:
            changed = legacy()
    except (StageError, subprocess.CalledProcessError) as error:
        sys.stderr.write(f'{error}\n')
        return 1
    print(f'changed={int(changed)}')
    return 0
