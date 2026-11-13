"""Отложенный откат фаервола на время стадии деплоя; файл запускается на узле сам по себе.

  python3 fwguard.py arm STAGE WINDOW  снимок ufw и своих nft-таблиц, сторож под systemd-run
  python3 fwguard.py watch WINDOW      сторож: нет новых ssh-входов дольше окна - откат к снимку
  python3 fwguard.py disarm STAGE      снять сторожа; 3 - фаервол уже откатан, 4 - сторожа нет

Пульс - `touch -c alive` новым ssh-входом каждые несколько секунд: открытое соединение стадии
живёт и при сломанном правиле (conntrack пропускает established). Сторож - отдельный юнит
systemd: он переживает обрыв ssh и убитый деплой. Только stdlib: на узле пакета может не быть.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(os.environ.get('FW_GUARD_DIR', '/run/vpn2-fw-guard'))
ETC = Path(os.environ.get('FW_GUARD_ETC', '/etc'))
UNIT = 'vpn2-fw-guard'
# Свои nft-таблицы стадий; чужие (Docker, fail2ban) не трогаются.
TABLES = ('inet ytzapret', 'inet anysda_ytquic', 'inet vpn2_hy2hop')
# Файлы под ETC, из которых эти таблицы поднимаются после перезагрузки.
FILES = ('nftables-hy2hop.conf',)


def say(text):
    print(f'vpn2-fw-guard: {text}', flush=True)


def ok(*command):
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def snapshot(snap):
    (snap / 'files').mkdir(parents=True)
    if shutil.which('ufw'):
        shutil.copytree(ETC / 'ufw', snap / 'ufw', symlinks=True)
        if (ETC / 'default/ufw').is_file():
            shutil.copy2(ETC / 'default/ufw', snap / 'default-ufw')
        status = subprocess.run(['ufw', 'status'], capture_output=True, text=True, env={**os.environ, 'LC_ALL': 'C'})
        if 'Status: active' in status.stdout.splitlines():
            (snap / 'ufw-active').touch()
    for table in TABLES if shutil.which('nft') else ():
        listed = subprocess.run(['nft', 'list', 'table', *table.split()], capture_output=True, text=True)
        if listed.returncode == 0:
            (snap / f'nft-{table.replace(" ", "-")}').write_text(listed.stdout)
    for name in FILES:
        if (ETC / name).is_file():
            shutil.copy2(ETC / name, snap / 'files' / name)


def restore(snap):
    good = True
    if (snap / 'ufw').is_dir():
        shutil.rmtree(ETC / 'ufw', ignore_errors=True)
        shutil.copytree(snap / 'ufw', ETC / 'ufw', symlinks=True)
        if (snap / 'default-ufw').is_file():
            shutil.copy2(snap / 'default-ufw', ETC / 'default/ufw')
    if shutil.which('ufw'):
        # enable на включённом ufw правил с диска не перечитывает - нужен reload.
        if (snap / 'ufw-active').exists():
            good &= ok('ufw', '--force', 'enable') and ok('ufw', 'reload')
        else:
            good &= ok('ufw', '--force', 'disable')
    for table in TABLES if shutil.which('nft') else ():
        ok('nft', 'delete', 'table', *table.split())
        saved = snap / f'nft-{table.replace(" ", "-")}'
        if saved.is_file():
            good &= ok('nft', '-f', str(saved))
    for name in FILES:
        saved, target = snap / 'files' / name, ETC / name
        try:
            if saved.is_file():
                shutil.copy2(saved, f'{target}.vpn2-guard')
                os.replace(f'{target}.vpn2-guard', target)
            else:
                target.unlink(missing_ok=True)
        except OSError:
            good = False
    return 0 if good else 1


def arm(stage, window):
    ok('systemctl', 'stop', UNIT)
    shutil.rmtree(DIR, ignore_errors=True)
    DIR.mkdir(mode=0o700, parents=True)
    (DIR / 'stage').write_text(f'{stage}\n')
    snapshot(DIR / 'snap')
    shutil.copy2(__file__, DIR / 'fwguard.py')
    (DIR / 'alive').touch()
    return subprocess.run(['systemd-run', '--quiet', '--collect', f'--unit={UNIT}', f'--setenv=FW_GUARD_DIR={DIR}',
                           f'--setenv=FW_GUARD_ETC={ETC}', sys.executable, '-I', str(DIR / 'fwguard.py'),
                           'watch', str(window)]).returncode


def watch(window):
    while True:
        time.sleep(2)
        try:
            if (DIR / 'decided').exists():
                return 0
            silent = int(time.time() - (DIR / 'alive').stat().st_mtime)
            if silent <= window:
                continue
            # decided - общий замок с disarm: решает тот, кто создал его первым.
            (DIR / 'decided').mkdir()
        except OSError:
            return 0
        stage = (DIR / 'stage').read_text().strip()
        say(f'новых ssh-входов нет {silent} с (окно {window} с), откатываю фаервол к снимку до стадии {stage}')
        code = restore(DIR / 'snap')
        (DIR / 'restored').write_text(f'{code}\n')
        return code


def disarm(stage):
    if not DIR.is_dir():
        say(f'сторож стадии {stage} не найден')
        return 4
    try:
        (DIR / 'decided').mkdir()
        decided = True
    except OSError:
        decided = False
    if not decided:
        for _ in range(120):
            if (DIR / 'restored').exists():
                break
            time.sleep(1)
        code = (DIR / 'restored').read_text().strip() if (DIR / 'restored').exists() else '?'
        lost = f'стадия {stage}: новые ssh-входы не проходили дольше окна'
        say(f'{lost}, ufw и таблицы стадий откатаны к состоянию до неё' if code == '0'
            else f'{lost}, откат фаервола не удался (код {code}), см. journalctl -u {UNIT}')
    ok('systemctl', 'stop', UNIT)
    shutil.rmtree(DIR, ignore_errors=True)
    return 0 if decided else 3


def main(argv):
    if len(argv) == 3 and argv[0] == 'arm':
        return arm(argv[1], int(argv[2]))
    if len(argv) == 2 and argv[0] == 'watch':
        return watch(int(argv[1]))
    if len(argv) == 2 and argv[0] == 'disarm':
        return disarm(argv[1])
    print(f'usage: {sys.argv[0]} arm STAGE WINDOW | watch WINDOW | disarm STAGE', file=sys.stderr)
    return 64


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
