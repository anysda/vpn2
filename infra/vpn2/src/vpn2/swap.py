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

    min(RAM, 2 ГиБ, свободно - min_free - 512 МиБ - install) вниз до МиБ, меньше 256 МиБ - без swap.
    install - место, которое установка ещё займёт после swap.
    """
    size = min(mem_kb * 1024, 2 * GIB)
    reason = f'RAM {mem_kb // 1024} МиБ'
    kept = f'{min_free // MIB} + 512' + (f', установке {install // MIB}' if install else '')
    cap = int((avail - min_free - RESERVE - install) / MIB) * MIB
    if cap < size:
        size, reason = cap, f'урезан, деплою оставлено {kept}'
    if size < 256 * MIB:
        return 0, f'swap пропущен: свободно {avail // MIB} МиБ, деплою нужно {kept}, swap меньше 256 МиБ не создаю'
    return size, f'swap: {SWAPFILE} {size // MIB} МиБ ({reason}; свободно {avail // MIB} МиБ)'


def pending(install_bytes, used):
    return max(0, install_bytes - used)


def min_free_bytes(manifest):
    try:
        return int(json.loads(Path(manifest).read_text())['min_free_bytes'])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_MIN_FREE


def _ok(argv):
    return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _usage():
    return shutil.disk_usage(os.path.dirname(SWAPFILE) or '/')


def _enable():
    os.chmod(SWAPFILE, 0o600)
    return _ok(['mkswap', SWAPFILE]) and _ok(['swapon', SWAPFILE])


def _create(mem_kb, min_free, install_bytes, say, *, zeros=False):
    """Создаёт и включает swap-файл по plan; False - пропущен."""
    usage = _usage()
    size, text = plan(mem_kb, usage.free, min_free, pending(install_bytes, usage.used))
    say(text)
    if not size:
        return False
    # fallocate на части ФС не работает или даёт файл с дырами, которые swapon не берёт, - тогда нулями.
    dd = ['dd', 'if=/dev/zero', f'of={SWAPFILE}', 'bs=1M', f'count={size // MIB}', 'status=none']
    for argv in ([dd] if zeros else [['fallocate', '-l', str(size), SWAPFILE], dd]):
        if _ok(argv) and _enable():
            return True
        Path(SWAPFILE).unlink(missing_ok=True)
    say(f'swap пропущен: swapon не включил {SWAPFILE}')
    return False


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


def _ours(proc):
    """Занято в нашем включённом файле; None - swap выключен; False - на узле чужой swap."""
    rows = [line.split() for line in (proc / 'swaps').read_text().splitlines()[1:]]
    active = [(row[0], int(row[3]) * 1024) for row in rows if len(row) >= 4]
    if not active:
        return None
    return active[0][1] if [path for path, _used in active] == [SWAPFILE] else False


def _drop_fstab(fstab):
    lines = fstab.read_text().splitlines(keepends=True)
    kept = [line for line in lines if line.split()[:1] != [SWAPFILE]]
    if kept != lines:
        fstab.write_text(''.join(kept))
    return kept != lines


def _shrink(proc, mem_kb, min_free, install_bytes, say, in_use, *, recreate=True):
    """Наш /swapfile, который держит место деплоя, убирается; True - убран. in_use None - файл выключен."""
    size, usage = os.path.getsize(SWAPFILE), _usage()
    fits, _ = plan(mem_kb, usage.free + size, min_free, pending(install_bytes, usage.used - size))
    if in_use is None:
        if size <= fits:
            return False
        say(f'swap: имеющийся {SWAPFILE} {size // MIB} МиБ не по месту, пересоздаю' if recreate
            else f'swap: выключенный {SWAPFILE} {size // MIB} МиБ занимает место деплоя, убираю')
        Path(SWAPFILE).unlink()
        return True
    # Пока запас деплоя не съеден или разница меньше шага, swap не пересоздаётся каждым деплоем.
    if usage.free >= min_free + RESERVE or size - fits < STEP:
        return False
    available = _meminfo(proc, 'MemAvailable') * 1024
    # swapoff возвращает страницы в память: не поместятся - узел встанет.
    if in_use + MEMORY_MARGIN > available:
        say(f'swap: {SWAPFILE} {size // MIB} МиБ занимает место деплоя, но выключить его нельзя: '
            f'в нём {in_use // MIB} МиБ, свободной памяти {available // MIB} МиБ; оставлен как есть')
        return False
    say(f'swap: включённый {SWAPFILE} {size // MIB} МиБ занимает место деплоя, '
        + (f'пересоздаю в {fits // MIB} МиБ' if fits else 'убираю'))
    if not _ok(['swapoff', SWAPFILE]):
        say(f'swap: swapoff {SWAPFILE} не прошёл, оставлен как есть')
        return False
    Path(SWAPFILE).unlink()
    return True


def release(min_free, say, *, proc='/proc', install_bytes=0):
    """Проверка места перед бэкапом: swap-файл v1.1.5, съевший запас деплоя, отдаёт лишнее."""
    proc = Path(proc)
    if not (proc / 'swaps').is_file() or not os.path.isfile(SWAPFILE) or _container():
        return False
    in_use, mem_kb = _ours(proc), _meminfo(proc, 'MemTotal')
    if in_use is False or not _shrink(proc, mem_kb, min_free, install_bytes, say, in_use, recreate=False):
        return False
    if in_use is not None and not _create(mem_kb, min_free, install_bytes, say):
        _drop_fstab(proc.parent / 'etc/fstab')
    return True


def ensure(manifest, say, *, root='/', install_bytes=0):
    """Включает swap, если он нужен и его нет; True - состояние узла изменилось.

    install_bytes - диск узла после полной установки: swap не берёт место, которое она займёт следом.
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
    if in_use is False:
        return False
    removed = os.path.isfile(SWAPFILE) and _shrink(root / 'proc', mem_kb, min_free, install_bytes, say, in_use)
    if in_use is not None and not removed:
        return False
    if os.path.isfile(SWAPFILE):
        # Обрывок прошлого прогона: файл есть, swap не включён.
        say(f'swap: включаю имеющийся {SWAPFILE}')
        os.chmod(SWAPFILE, 0o600)
        on = _ok(['swapon', SWAPFILE]) or _enable()
        if not on:
            say(f'swap: {SWAPFILE} не включается, пересоздаю')
            Path(SWAPFILE).unlink(missing_ok=True)
            on = _create(mem_kb, min_free, install_bytes, say, zeros=True)
    else:
        on = _create(mem_kb, min_free, install_bytes, say)
    if not on:
        # Файла нет и не будет: строка fstab о нём давала бы сбой юнита при загрузке.
        return _drop_fstab(root / 'etc/fstab') or removed
    fstab = root / 'etc/fstab'
    if not any(line.split()[:1] == [SWAPFILE] for line in fstab.read_text().splitlines()):
        with fstab.open('a') as output:
            output.write(f'{SWAPFILE} none swap sw 0 0\n')
    (root / 'etc/sysctl.d/60-anysda-swap.conf').write_text('vm.swappiness = 10\n')
    subprocess.run(['sysctl', '-q', '-w', 'vm.swappiness=10'], stdout=subprocess.DEVNULL, check=False)
    return True
