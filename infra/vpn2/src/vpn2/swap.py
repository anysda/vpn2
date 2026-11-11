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
RESERVE = 512 * MIB        # запас деплою сверх порога проверки перед бэкапом
STEP = 256 * MIB           # меньшей разницы ради включённый swap не пересоздаём
MEMORY_MARGIN = 128 * MIB  # остаётся свободным в памяти после swapoff
ENTRY_INSTALL = 6 * GIB    # диск entry после полной установки; то же число в роли base


def plan(mem_kb, avail, min_free, install=0):
    """Размер swap в байтах и пояснение; 0 - не создавать.

    min(RAM, 2 ГиБ, свободно - min_free - 512 МиБ - install), округление вниз
    до МиБ, меньше 256 МиБ - без swap. install - место, которое установка ещё
    не заняла: swap создаётся первым шагом, пакеты и образы ложатся после него.
    """
    size = min(mem_kb * 1024, 2 * GIB)
    reason = f'RAM {mem_kb // 1024} МиБ'
    kept = f'{min_free // MIB} + 512' + (f', установке {install // MIB}' if install else '')
    cap = _trunc((avail - min_free - RESERVE - install) / MIB) * MIB
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


def _create(path, mem_kb, min_free, say, install_bytes=0, *, zeros=False):
    """Создаёт и включает swap-файл; False - пропущен."""
    usage = shutil.disk_usage(os.path.dirname(path) or '/')
    size, text = plan(mem_kb, usage.free, min_free, pending(install_bytes, usage.used))
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


def _oversized(path, mem_kb, min_free, install_bytes):
    """Выключенный файл больше, чем ему сейчас отвёл бы plan: размер в байтах, иначе 0."""
    size = os.path.getsize(path)
    usage = shutil.disk_usage(os.path.dirname(path) or '/')
    fits, _ = plan(mem_kb, usage.free + size, min_free,
                   pending(install_bytes, usage.used - size))
    return size if size > fits else 0


def _container():
    # В privileged LXC swapon берёт файл гостя в swap хоста: контейнеру swap не даём.
    if not shutil.which('systemd-detect-virt'):
        return ''
    virt = subprocess.run(['systemd-detect-virt', '--container'], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
    return '' if virt == 'none' else virt


def _meminfo(proc, key):
    return next(int(line.split()[1]) for line in (proc / 'meminfo').read_text().splitlines()
                if line.startswith(key + ':'))


def _active(proc):
    """Включённые swap из /proc/swaps: (путь, занято байт)."""
    rows = [line.split() for line in (proc / 'swaps').read_text().splitlines()[1:]]
    return [(row[0], int(row[3]) * 1024) for row in rows if len(row) >= 4]


def _drop_fstab(fstab, path):
    lines = fstab.read_text().splitlines(keepends=True)
    kept = [line for line in lines if line.split()[:1] != [path]]
    if kept != lines:
        fstab.write_text(''.join(kept))
    return kept != lines


def _shrink(proc, min_free, install_bytes, say, in_use):
    """Наш /swapfile прежнего размера отдаёт место деплою; True - узел изменён.

    Старые версии брали под swap всё свободное, и проверке перед бэкапом места
    не оставалось. Файл трогаем, только когда запас деплоя (min_free + 512 МиБ)
    уже съеден и расчётный размер меньше хотя бы на шаг: иначе по мере
    заполнения диска swap пересоздавался бы каждым деплоем. in_use - байт
    занято во включённом файле, None - файл выключен.
    """
    path = SWAPFILE
    size = os.path.getsize(path)
    usage = shutil.disk_usage(os.path.dirname(path) or '/')
    if usage.free >= min_free + RESERVE:
        return False
    fits, _ = plan(_meminfo(proc, 'MemTotal'), usage.free + size, min_free,
                   pending(install_bytes, usage.used - size))
    if size - fits < STEP:
        return False
    if in_use is None:
        say(f'swap: выключенный {path} {size // MIB} МиБ занимает место деплоя, убираю')
        Path(path).unlink()
        return True
    available = _meminfo(proc, 'MemAvailable') * 1024
    # swapoff возвращает страницы в память: не поместятся - узел встанет.
    if in_use + MEMORY_MARGIN > available:
        say(f'swap: {path} {size // MIB} МиБ занимает место деплоя, но выключить его нельзя: '
            f'в нём {in_use // MIB} МиБ, свободной памяти {available // MIB} МиБ; оставлен как есть')
        return False
    say(f'swap: включённый {path} {size // MIB} МиБ занимает место деплоя, '
        + (f'пересоздаю в {fits // MIB} МиБ' if fits else 'убираю'))
    if not _ok(['swapoff', path]):
        say(f'swap: swapoff {path} не прошёл, оставлен как есть')
        return False
    Path(path).unlink()
    return True


def _ours(proc):
    """Занято в нашем включённом файле; None - swap выключен; False - на узле чужой swap."""
    active = _active(proc)
    if not active:
        return None
    if [path for path, _used in active] != [SWAPFILE]:
        return False
    return active[0][1]


def release(min_free, say, *, proc='/proc', install_bytes=0):
    """Перед проверкой места деплоем: наш swap-файл прежнего размера отдаёт лишнее.

    Зовёт проверка перед бэкапом, когда свободного меньше порога: узел,
    поставленный старой версией, чинится сам, без ручного swapoff.
    """
    proc = Path(proc)
    if not (proc / 'swaps').is_file() or not os.path.isfile(SWAPFILE) or _container():
        return False
    in_use = _ours(proc)
    if in_use is False or not _shrink(proc, min_free, install_bytes, say, in_use):
        return False
    if in_use is not None:
        _recreate(proc.parent, _meminfo(proc, 'MemTotal'), min_free, install_bytes, say)
    return True


def _recreate(root, mem_kb, min_free, install_bytes, say):
    """После снятого swap: новый по расчёту; места нет - убирает и строку fstab."""
    if _create(SWAPFILE, mem_kb, min_free, say, install_bytes):
        return True
    _drop_fstab(root / 'etc/fstab', SWAPFILE)
    return False


def _switch_on(mem_kb, min_free, install_bytes, say):
    """Swap выключен: создаёт файл или включает имеющийся; False - узел остался без swap."""
    extra = _oversized(SWAPFILE, mem_kb, min_free, install_bytes) if os.path.isfile(SWAPFILE) else 0
    if extra:
        # Выключенный файл прежнего размера оставил бы деплой без места.
        say(f'swap: имеющийся {SWAPFILE} {extra // MIB} МиБ не по месту, пересоздаю')
        Path(SWAPFILE).unlink()
    if not os.path.isfile(SWAPFILE):
        return _create(SWAPFILE, mem_kb, min_free, say, install_bytes)
    # Обрывок прошлого прогона: файл есть, swap не включён.
    say(f'swap: включаю имеющийся {SWAPFILE}')
    os.chmod(SWAPFILE, 0o600)
    if _ok(['swapon', SWAPFILE]) or _enable(SWAPFILE):
        return True
    say(f'swap: {SWAPFILE} не включается, пересоздаю')
    Path(SWAPFILE).unlink(missing_ok=True)
    return _create(SWAPFILE, mem_kb, min_free, say, install_bytes, zeros=True)


def ensure(manifest, say, *, root='/', install_bytes=0):
    """Включает swap, если он нужен и его нет; True - состояние узла изменилось.

    install_bytes - сколько диска занимает узел после полной установки: на
    чистом узле swap не берёт место, которое установка займёт следом.
    """
    root = Path(root)
    virt = _container()
    if virt:
        say(f'swap пропущен: контейнер ({virt})')
        return False
    mem_kb = _meminfo(root / 'proc', 'MemTotal')
    if mem_kb >= 3840 * 1024:
        return False
    min_free = min_free_bytes(manifest)
    in_use = _ours(root / 'proc')
    if in_use is not None:
        # Включённый swap не трогаем, кроме своего файла, который держит место деплоя.
        if in_use is False or not os.path.isfile(SWAPFILE) \
                or not _shrink(root / 'proc', min_free, install_bytes, say, in_use):
            return False
        if not _recreate(root, mem_kb, min_free, install_bytes, say):
            return True
    elif not _switch_on(mem_kb, min_free, install_bytes, say):
        # Файла нет и не будет: строка fstab о нём давала бы сбой юнита при загрузке.
        return not os.path.isfile(SWAPFILE) and _drop_fstab(root / 'etc/fstab', SWAPFILE)
    fstab = root / 'etc/fstab'
    if not any(line.split()[:1] == [SWAPFILE] for line in fstab.read_text().splitlines()):
        with fstab.open('a') as output:
            output.write(f'{SWAPFILE} none swap sw 0 0\n')
    (root / 'etc/sysctl.d/60-anysda-swap.conf').write_text('vm.swappiness = 10\n')
    subprocess.run(['sysctl', '-q', '-w', 'vm.swappiness=10'], stdout=subprocess.DEVNULL, check=False)
    return True
