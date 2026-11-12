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

from vpn2 import entryvpn, exitnode, firewall, ikev2, pki, svc, swap

CHANGED = 'vpn2: changed'


def _say(text):
    print(f"[{os.environ.get('HOST_TAG', 'anysda')}]   {text}", flush=True)


def _ufw(args):
    """Политика, свои правила по комментариям, включение и журнал ufw."""
    changed = False
    if not args.only_rules:
        changed = firewall.ufw_repair_if_lost()
        changed = bool(firewall.ufw_defaults()) or changed
    for group in json.loads(args.groups):
        rules = list(group.get('rules', ()))
        if 'dns' in group:
            rules += entryvpn.dns_rules(group['dns']['to'], group['dns']['subnets'])
        added, deleted = firewall.ufw_reconcile(group['comment'], rules, group.get('aliases', ()))
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
    else:
        # Каталог прежних выпусков: обмен именами одним системным вызовом, на
        # любой coreutils; два переименования - только где обмена нет.
        try:
            pki._exchange(fresh, link)
            shutil.rmtree(fresh)
        except OSError:
            link.rename(aside)
            fresh.replace(link)
    for old in builds.iterdir():
        if old.name != venv.name and not _venv_in_use(old):
            shutil.rmtree(old)
    return True


def _venv_in_use(build):
    """Путь сборки стоит в командной строке живого процесса.

    Сверяется полный путь: сборка с тем же именем в чужом каталоге - не эта.
    """
    needles = {os.fsencode(f'{path}/') for path in (build.absolute(), build.resolve())}
    for item in glob.glob('/proc/[0-9]*/cmdline'):
        try:
            line = Path(item).read_bytes()
        except OSError:
            continue
        if any(needle in line for needle in needles):
            return True
    return False


def ovpn_restart(*, directory=entryvpn.OVPN_DIR, applied=svc.APPLIED, apply=entryvpn.OVPN_APPLY):
    """Рестарт OpenVPN по общему со стадией панели отпечатку входов."""
    return entryvpn.ovpn_restart(
        lambda: svc.restart_if_changed(entryvpn.OVPN_UNIT, entryvpn.ovpn_inputs(directory), applied),
        _say, apply=apply)


def _arg(*flags, **options):
    return flags, options


# действие, справка, аргументы, обработчик: он возвращает, менялся ли узел
ACTIONS = (
    ('nft-preserve', 'flush ruleset в nftables.conf закомментирован', (),
     lambda _: firewall.preserve_nftables_conf()),
    ('swap', 'swap узлу с памятью меньше 4 ГБ', (
        _arg('--manifest', default='/tmp/anysda/release-manifest.json'),
        _arg('--install-bytes', type=int, default=0, help='место на диске после полной установки узла, байт'),
    ), lambda args: swap.ensure(args.manifest, _say, install_bytes=args.install_bytes)),
    ('ufw', 'политика и правила ufw по группам', (
        _arg('--only-rules', action='store_true', help='только правила групп: политику и включение ufw не трогать'),
        _arg('groups', help='JSON: [{"comment", "rules", "aliases", "dns": {"to", "subnets"}}]'),
    ), _ufw),
    ('f2b-ignore', 'адреса, которые fail2ban не банит', (_arg('entry_host', nargs='?', default=''),), _f2b_ignore),
    ('restart-if-changed', 'перезапуск службы при смене её входов', (
        _arg('--record', help='файл отпечатка вместо общего каталога'),
        _arg('unit'),
        _arg('file', nargs='+'),
    ), lambda args: svc.restart_if_changed(args.unit, args.file, record=args.record)),
    ('exit-config', 'конфиг sing-box экзита', (_arg('--template', required=True),),
     lambda args: exitnode.configure(os.environ, args.template, _say)),
    ('wg-identity', 'ключи сервера WireGuard и начальный wg0.conf', (
        _arg('--port', required=True),
        _arg('--address', required=True),
    ), lambda args: entryvpn.wg_identity(args.port, args.address)),
    ('wg-apply', 'wg0.conf в ядро без лишнего рестарта', (), lambda _: entryvpn.wg_apply(_say)),
    ('ovpn-pki', 'каталоги и комплект PKI сервера OpenVPN', (), lambda _: entryvpn.ovpn_pki()),
    ('ovpn-restart', 'рестарт OpenVPN при смене входов и отметка last good', (), lambda _: ovpn_restart()),
    ('ikev2-server', 'сертификаты, конфиг swanctl и загрузка в charon', (
        _arg('--entry-host', required=True),
        _arg('--public-host', required=True, help='адрес сервера в профилях'),
        _arg('--panel-domain', default=''),
        _arg('--subnet', required=True),
        _arg('--dns', required=True),
    ), lambda args: ikev2.server(args.entry_host, args.public_host, args.panel_domain, args.subnet, args.dns,
                                 _say, lambda: svc.restart_if_changed(ikev2.UNIT, ikev2.starter_inputs()))),
    ('venv', 'venv vpn2 экзита из колеса', (
        _arg('wheel'),
        _arg('--root', default='/opt/vpn2'),
        _arg('--pip-dir', default='/usr/share/python-wheels'),
        _arg('--python', default='/usr/bin/python3'),
    ), _venv),
)


def main(argv):
    parser = argparse.ArgumentParser(prog='vpn2 host', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='action', required=True)
    for name, text, arguments, handler in ACTIONS:
        item = sub.add_parser(name, help=text)
        for flags, options in arguments:
            item.add_argument(*flags, **options)
        item.set_defaults(run=handler)
    args = parser.parse_args(argv)
    try:
        changed = args.run(args)
    except (RuntimeError, ValueError, OSError, pki.PkiError, subprocess.CalledProcessError) as error:
        print(f'vpn2 host {args.action}: {error}', file=sys.stderr)
        return 1
    if changed:
        print(CHANGED)
    return 0


if __name__ == '__main__':
    # Чистый экзит зовёт модуль прямо из колеса: `python3 -m vpn2.cli.host`.
    sys.exit(main(sys.argv[1:]))
