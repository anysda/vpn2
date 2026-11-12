"""Листинг бэкапов: локальные архивы и S3."""
import os
import re

from vpn2.backup import archive, env

ROW = '%-50s %-22s %12s'


def human_size(n):
    """Размер как `numfmt --to=iec`: до 1024 число, дальше округление вверх до 1 знака (до 10) или целого."""
    if n < 1024:
        return str(n)
    for i, unit in enumerate('KMGTPEZY', 1):
        d = 1024 ** i
        if n < 10 * d:
            t = -(-n * 10 // d)
            return f'{t // 10}.{t % 10}{unit}' if t < 100 else f'{t // 10}{unit}'
        if n < 1024 * d:
            v = -(-n // d)
            return f'{v}{unit}' if v < 1024 else f'1.0{"MGTPEZY"[i - 1]}'
    return str(n)


def local_rows(local_dir):
    """[(имя, метка или '?', размер в байтах)], свежие по mtime первыми."""
    found = []
    try:
        entries = list(os.scandir(local_dir))
    except OSError:
        return []
    for entry in entries:
        try:
            if archive.is_archive(entry):
                st = entry.stat(follow_symlinks=False)
                found.append((st.st_mtime_ns, entry.name, st.st_size))
        except OSError:
            continue
    return [(name, (re.search(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z', name) or ['?'])[0], size)
            for _, name, size in sorted(found, reverse=True)]


def render(settings):
    """Печать листинга; код возврата: 0, а для s3 - 1, если rclone упал или архивов нет."""
    print(ROW % ('Name', 'Timestamp (UTC)', 'Size'))
    print(ROW % ('----', '---------------', '----'))
    for name, ts, size in local_rows(settings.local_dir):
        print(ROW % (name, ts, human_size(size)))
    if settings.backend != 's3':
        return 0
    print(f'\n── S3: s3://{settings.s3_bucket}/ ──', flush=True)
    code, rows, _why = env.s3_names(settings)
    for name, date, clock, size in sorted(rows, key=lambda row: (row[1], row[2], row[0]), reverse=True):
        print('  ' + ROW % (name, f'{date}T{clock}Z', size))
    return 0 if code == 0 and rows else 1
