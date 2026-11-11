"""Swap узла: /swapfile хосту с памятью меньше 4 ГБ, место деплою не занимает."""
import json
import os
import shutil
import subprocess
from pathlib import Path

MIB = 1024 ** 2
GIB = 1024 ** 3
DEFAULT_MIN_FREE = 2 * GIB  # то же число, что в infra/lib/build-release.py
SWAPFILE = '/swapfile'


def plan(mem_kb, avail, min_free, install=0):
    """Размер swap в байтах и пояснение; 0 - не создавать.

    min(RAM, 2 ГиБ, свободно - min_free - 512 МиБ - install), округление вниз
    до МиБ, меньше 256 МиБ - без swap. install - место, которое установка ещё
    не заняла: swap создаётся первым шагом, пакеты и образы ложатся после него.
    """
    size = min(mem_kb * 1024, 2 * GIB)
    reason = f'RAM {mem_kb // 1024} МиБ'
    kept = f'{min_free // MIB} + 512' + (f', установке {install // MIB}' if install else '')
    cap = _trunc((avail - min_free - 512 * MIB - install) / MIB) * MIB
    if cap < size:
        size = cap
        reason = f'урезан, деплою оставлено {kept}'
    if size < 256 * MIB:
        return 0, (f'swap пропущен: свободно {avail // MIB} МиБ, деплою нужно '
                   f'{kept}, swap меньше 256 МиБ не создаю')
    return size, f'swap: {SWAPFILE} {size // MIB} МиБ ({reason}; свободно {avail // MIB} МиБ)'


def pending(install_bytes, used):
    """Сколько установка ещё займёт: её полный размер минус уже занятое на диске."""
    return max(0, install_bytes - used)


def _trunc(value):
    # bash делит с усечением к нулю, а не вниз
    return int(value)


def min_free_bytes(manifest):
    try:
        return int(json.loads(Path(manifest).read_text())['min_free_bytes'])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_MIN_FREE


def _ok(argv):
    return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _enable(path):
    os.chmod(path, 0o600)
    return _ok(['mkswap', path]) and _ok(['swapon', path])


def _create(path, mem_kb, manifest, say, install_bytes=0, *, zeros=False):
    """Создаёт и включает swap-файл; False - пропущен."""
    usage = shutil.disk_usage(os.path.dirname(path) or '/')
    size, text = plan(mem_kb, usage.free, min_free_bytes(manifest), pending(install_bytes, usage.used))
    say(text)
    if not size:
        return False
    # fallocate на части ФС не работает или даёт файл с дырами, которые swapon
    # не берёт, - тогда пишем нулями.
    if not zeros and _ok(['fallocate', '-l', str(size), path]) and _enable(path):
        return True
    Path(path).unlink(missing_ok=True)
    if _ok(['dd', 'if=/dev/zero', f'of={path}', 'bs=1M', f'count={size // MIB}', 'status=none']) \
            and _enable(path):
        return True
    Path(path).unlink(missing_ok=True)
    say(f'swap пропущен: swapon не включил {path}')
    return False


def _oversized(path, mem_kb, manifest, install_bytes):
    """Выключенный файл больше, чем ему сейчас отвёл бы plan: размер в байтах, иначе 0."""
    size = os.path.getsize(path)
    usage = shutil.disk_usage(os.path.dirname(path) or '/')
    fits, _ = plan(mem_kb, usage.free + size, min_free_bytes(manifest),
                   pending(install_bytes, usage.used - size))
    return size if size > fits else 0


def ensure(manifest, say, *, root='/', install_bytes=0):
    """Включает swap, если он нужен и его нет; True - состояние узла изменилось.

    install_bytes - сколько диска занимает узел после полной установки: на
    чистом узле swap не берёт место, которое установка займёт следом.
    """
    root = Path(root)
    # В privileged LXC swapon берёт файл гостя в swap хоста: контейнеру swap не даём.
    if shutil.which('systemd-detect-virt'):
        virt = subprocess.run(['systemd-detect-virt', '--container'], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
        if virt and virt != 'none':
            say(f'swap пропущен: контейнер ({virt})')
            return False
    mem_kb = next(int(line.split()[1]) for line in (root / 'proc/meminfo').read_text().splitlines()
                  if line.startswith('MemTotal:'))
    if mem_kb >= 3840 * 1024:
        return False
    if len((root / 'proc/swaps').read_text().splitlines()) > 1:
        return False
    extra = _oversized(SWAPFILE, mem_kb, manifest, install_bytes) if os.path.isfile(SWAPFILE) else 0
    if extra:
        # Выключенный файл прежнего размера оставил бы деплой без места.
        say(f'swap: имеющийся {SWAPFILE} {extra // MIB} МиБ не по месту, пересоздаю')
        Path(SWAPFILE).unlink()
    if not os.path.isfile(SWAPFILE):
        if not _create(SWAPFILE, mem_kb, manifest, say, install_bytes):
            return False
    else:
        # Обрывок прошлого прогона: файл есть, swap не включён.
        say(f'swap: включаю имеющийся {SWAPFILE}')
        os.chmod(SWAPFILE, 0o600)
        if not _ok(['swapon', SWAPFILE]) and not _enable(SWAPFILE):
            say(f'swap: {SWAPFILE} не включается, пересоздаю')
            Path(SWAPFILE).unlink(missing_ok=True)
            if not _create(SWAPFILE, mem_kb, manifest, say, install_bytes, zeros=True):
                return False
    fstab = root / 'etc/fstab'
    if not any(line.split()[:1] == [SWAPFILE] for line in fstab.read_text().splitlines()):
        with fstab.open('a') as output:
            output.write(f'{SWAPFILE} none swap sw 0 0\n')
    (root / 'etc/sysctl.d/60-anysda-swap.conf').write_text('vm.swappiness = 10\n')
    subprocess.run(['sysctl', '-q', '-w', 'vm.swappiness=10'], stdout=subprocess.DEVNULL, check=False)
    return True
