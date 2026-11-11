"""vpn2 host <действие> - шаги настройки узла для ролей ansible.

Действие, изменившее узел, печатает последней строкой `vpn2: changed`:
по ней роль ставит changed. Модуль не тянет PyYAML: на чистом экзите он
запускается из колеса системным python до установки пакетов.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from vpn2 import apt, exitnode, firewall, swap

CHANGED = 'vpn2: changed'
APPLIED_DIR = Path('/var/lib/anysda/applied')


def _say(text):
    print(f"[{os.environ.get('HOST_TAG', 'anysda')}]   {text}", flush=True)


def _packages(args):
    return apt.install(args.package, docker=args.docker)


NFT_DROPIN = '''[Service]
# Stopping this loader must not remove rules owned by other services.
ExecStop=
'''


def nft_preserve(*, root='/', run=subprocess.run):
    """nftables не сбрасывает чужие таблицы: ни из конфига, ни остановкой юнита."""
    root = Path(root)
    changed = firewall.preserve_nftables_conf(root / 'etc/nftables.conf')
    dropin = root / 'etc/systemd/system/nftables.service.d/10-anysda-preserve.conf'
    if not dropin.is_file() or dropin.read_text() != NFT_DROPIN:
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(NFT_DROPIN)
        run(['systemctl', 'daemon-reload'], check=True)
        changed = True
    return changed


def _nft_preserve(args):
    del args
    return nft_preserve()


def _swap(args):
    return swap.ensure(args.manifest, _say)


def _ufw(args):
    """Политика, свои правила по комментариям, включение и журнал ufw."""
    changed = False
    if not args.only_rules:
        changed = firewall.ufw_repair_if_lost()
        changed = bool(firewall.ufw_defaults()) or changed
    for group in json.loads(args.groups):
        added, deleted = firewall.ufw_reconcile(group['comment'], group.get('rules', ()),
                                                group.get('aliases', ()))
        for sign, rules in (('+', added), ('-', deleted)):
            for rule in rules:
                _say(f"ufw {sign} {rule} ({group['comment']})")
        changed = changed or bool(added or deleted)
    if args.only_rules:
        return changed
    if not firewall.ufw_active():
        subprocess.run(['ufw', '--force', 'enable'], check=True, stdout=subprocess.DEVNULL)
        changed = True
    # Без журнала: иначе каждый пакет сканера портов - строка в kern.log.
    try:
        logging_off = 'LOGLEVEL=off' in Path('/etc/ufw/ufw.conf').read_text().splitlines()
    except OSError:
        logging_off = False
    if not logging_off:
        subprocess.run(['ufw', 'logging', 'off'], check=True, stdout=subprocess.DEVNULL)
        changed = True
    return changed


def f2b_ignore(client, entry_host, resolve=None):
    """Адреса, которые fail2ban не банит: loopback, источник деплоя, адреса entry."""
    result = ['127.0.0.1/8', '::1']

    def add(address):
        if not re.fullmatch(r'[0-9A-Fa-f:.]+', address) or not re.search(r'[.:]', address):
            return
        if address.startswith('127.') or address == '::1' or address in result:
            return
        result.append(address)

    add(client.split(' ')[0])
    if re.fullmatch(r'[0-9A-Fa-f:.]+', entry_host):
        add(entry_host)
    elif entry_host:
        for address in (resolve or _resolve)(entry_host):
            add(address)
    return result


def _resolve(host):
    try:
        return [item[4][0] for item in socket.getaddrinfo(host, None)]
    except OSError:
        return []


def _f2b_ignore(args):
    print(' '.join(f2b_ignore(os.environ.get('SSH_CLIENT', ''), args.entry_host)))
    return False


def fingerprint(paths):
    """sha256 склейки содержимого; отсутствующий файл в отпечаток не входит."""
    digest = hashlib.sha256()
    for path in paths:
        try:
            digest.update(Path(path).read_bytes())
        except OSError:
            continue
    return digest.hexdigest()


def restart_if_changed(unit, paths, *, applied=None, record=None, run=subprocess.run):
    """Перезапуск службы, только если сменились её входы или она не работает."""
    if record is None:
        record = (APPLIED_DIR if applied is None else Path(applied)) / f'{unit}.sha256'
    record = Path(record)
    current = fingerprint(paths)
    active = run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0
    try:
        known = record.read_text().strip()
    except OSError:
        known = None
    if active and known == current:
        return False
    run(['systemctl', 'restart', unit], check=True)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(current + '\n')
    return True


def _restart_if_changed(args):
    changed = restart_if_changed(args.unit, args.file, record=args.record)
    _say(f'{args.unit} перезапущен' if changed
         else f'{args.unit}: конфиг, сертификаты и бинарник прежние — не перезапускаю')
    return changed


def _venv(args):
    """venv vpn2 экзита из колеса, без сети; PyYAML берётся из системных пакетов."""
    root = Path(args.root)
    mark = root / 'venv/.vpn2-wheel'
    digest = hashlib.sha256(Path(args.wheel).read_bytes()).hexdigest()
    if mark.is_file() and mark.read_text().strip() == digest:
        return False
    pips = sorted(glob.glob(os.path.join(args.pip_dir, 'pip-*.whl')))
    if not pips:
        raise RuntimeError(f'нет pip-*.whl в {args.pip_dir} (пакет python3-venv)')
    # Сборка ставится рядом и подменяет действующую одним переименованием:
    # сторож или таймер, зовущий venv/bin/vpn2, не застаёт пустой каталог.
    link = root / 'venv'
    builds = root / 'venvs'
    venv = builds / digest[:16]
    if venv.exists():
        shutil.rmtree(venv)
    builds.mkdir(parents=True, exist_ok=True)
    python = args.python
    subprocess.run([python, '-m', 'venv', '--without-pip', '--system-site-packages',
                    str(venv)], check=True)
    subprocess.run([python, pips[-1] + '/pip', '--python', str(venv / 'bin/python'),
                    'install', '--no-index', '--no-deps', '--disable-pip-version-check',
                    args.wheel], check=True, stdout=subprocess.DEVNULL)
    (venv / '.vpn2-wheel').write_text(digest + '\n')
    fresh = root / 'venv.new'
    aside = builds / '.replaced'
    for leftover in fresh, aside:
        if leftover.is_symlink():
            leftover.unlink()
        elif leftover.exists():
            shutil.rmtree(leftover)
    fresh.symlink_to(f'venvs/{venv.name}')
    if not link.is_dir() or link.is_symlink():
        fresh.replace(link)
    elif subprocess.run(['mv', '--exchange', '-T', str(fresh), str(link)],
                        stderr=subprocess.DEVNULL).returncode == 0:
        # Каталог прежних выпусков: обмен именами одним вызовом (coreutils 9.5).
        shutil.rmtree(fresh)
    else:
        link.rename(aside)
        fresh.replace(link)
    for old in builds.iterdir():
        if old.name != venv.name and not _venv_in_use(old.name):
            shutil.rmtree(old)
    return True


def _venv_in_use(name):
    """Путь сборки стоит в командной строке живого процесса."""
    needle = f'/venvs/{name}/'.encode()
    for item in glob.glob('/proc/[0-9]*/cmdline'):
        try:
            if needle in Path(item).read_bytes():
                return True
        except OSError:
            continue
    return False


SSHD_HEADER = '# anysda-vpn — set by infra/scripts/00-bootstrap.sh'


def sshd_auth(password, root):
    """Drop-in и те же строки основного конфига: парольный вход yes или no."""
    root_login = 'yes' if password == 'yes' else 'prohibit-password'
    dropin = root / 'etc/ssh/sshd_config.d/00-anysda.conf'
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text(f'{SSHD_HEADER}\nPasswordAuthentication {password}\n'
                      f'PermitRootLogin {root_login}\nKbdInteractiveAuthentication no\n')
    config = root / 'etc/ssh/sshd_config'
    text = config.read_text()
    for key, value in (('PasswordAuthentication', password), ('PermitRootLogin', root_login)):
        text = re.sub(rf'(?m)^#?{key}.*$', f'{key} {value}', text)
    config.write_text(text)


def sshd(password, *, root='/', run=subprocess.run):
    """Настройка входа по ssh; невалидный конфиг откатывается к парольному входу."""
    root = Path(root)
    config = root / 'etc/ssh/sshd_config'
    penalties = root / 'etc/ssh/sshd_config.d/99-anysda-no-penalties.conf'
    watched = (config, root / 'etc/ssh/sshd_config.d/00-anysda.conf', penalties)

    def state():
        return [path.read_bytes() if path.is_file() else None for path in watched]

    before = state()
    sshd_auth(password, root)
    text = config.read_text()
    for key in ('ChallengeResponseAuthentication', 'KbdInteractiveAuthentication'):
        text = re.sub(rf'(?m)^#?{key}.*$', f'{key} no', text)
    config.write_text(text)
    # sshd -t и -T требуют каталог privsep, а apt при обновлении openssh его
    # удаляет: до перезапуска ssh.service вернуть его некому.
    (root / 'run/sshd').mkdir(mode=0o755, parents=True, exist_ok=True)
    # OpenSSH 9.8+ штрафует адрес на 90 с за оборванный вход, и несколько неудачных
    # входов деплоя запирают его. Старый sshd директивы не знает и с ней падает.
    dump = run(['sshd', '-T'], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    if any(line.lower().startswith('persourcepenalties ') for line in dump.splitlines()):
        penalties.write_text('PerSourcePenalties no\n')
    else:
        penalties.unlink(missing_ok=True)
    if run(['sshd', '-t']).returncode != 0:
        sshd_auth('yes', root)
        penalties.unlink(missing_ok=True)
        raise RuntimeError('конфиг sshd невалиден — откатываюсь')
    changed = state() != before
    if changed or run(['systemctl', 'is-active', '--quiet', 'ssh']).returncode != 0:
        run(['systemctl', 'reload-or-restart', 'ssh'], check=True)
    return changed


def _exit_config(args):
    return exitnode.configure(os.environ, args.template, _say)


def _exit_hy2hop(args):
    del args
    return exitnode.hy2hop(os.environ, _say)


def _sshd(args):
    return sshd(args.password)


def main(argv):
    parser = argparse.ArgumentParser(prog='vpn2 host', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='action', required=True)
    item = sub.add_parser('packages')
    item.add_argument('--docker', action='store_true')
    item.add_argument('package', nargs='+')
    item.set_defaults(run=_packages)
    sub.add_parser('nft-preserve').set_defaults(run=_nft_preserve)
    item = sub.add_parser('swap')
    item.add_argument('--manifest', default='/tmp/anysda/release-manifest.json')
    item.set_defaults(run=_swap)
    item = sub.add_parser('ufw')
    item.add_argument('--only-rules', action='store_true',
                      help='только правила групп: политику и включение ufw не трогать')
    item.add_argument('groups', help='JSON: [{"comment", "rules", "aliases"}]')
    item.set_defaults(run=_ufw)
    item = sub.add_parser('f2b-ignore')
    item.add_argument('entry_host', nargs='?', default='')
    item.set_defaults(run=_f2b_ignore)
    item = sub.add_parser('restart-if-changed')
    item.add_argument('--record', help='файл отпечатка вместо общего каталога')
    item.add_argument('unit')
    item.add_argument('file', nargs='+')
    item.set_defaults(run=_restart_if_changed)
    item = sub.add_parser('exit-config')
    item.add_argument('--template', required=True)
    item.set_defaults(run=_exit_config)
    sub.add_parser('exit-hy2hop').set_defaults(run=_exit_hy2hop)
    item = sub.add_parser('sshd')
    item.add_argument('password', choices=('yes', 'no'), help='парольный вход')
    item.set_defaults(run=_sshd)
    item = sub.add_parser('venv')
    item.add_argument('wheel')
    item.add_argument('--root', default='/opt/vpn2')
    item.add_argument('--pip-dir', default='/usr/share/python-wheels')
    item.add_argument('--python', default='/usr/bin/python3')
    item.set_defaults(run=_venv)
    args = parser.parse_args(argv)
    try:
        changed = args.run(args)
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'vpn2 host {args.action}: {error}', file=sys.stderr)
        return 1
    if changed:
        print(CHANGED)
    return 0


if __name__ == '__main__':
    # Чистый экзит зовёт модуль прямо из колеса: `python3 -m vpn2.cli.host`.
    sys.exit(main(sys.argv[1:]))
