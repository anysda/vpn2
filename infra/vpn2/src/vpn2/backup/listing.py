"""Листинг бэкапов: локальные архивы и S3."""
import os
import re
import subprocess

from vpn2.backup import archive, env

ROW = '%-50s %-22s %12s'


def human_sizes(sizes):
    """Размеры в байтах так, как их пишет `numfmt --to=iec`."""
    done = subprocess.run(['numfmt', '--to=iec'], input=''.join(f'{size}\n' for size in sizes),
                          capture_output=True, text=True, check=True)
    return done.stdout.split()


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
    rows = local_rows(settings.local_dir)
    for (name, ts, _size), size in zip(rows, human_sizes(size for *_, size in rows)):
        print(ROW % (name, ts, size))
    if settings.backend != 's3':
        return 0
    print(f'\n── S3: s3://{settings.s3_bucket}/ ──', flush=True)
    code, rows, _why = env.s3_names(settings)
    for name, date, clock, size in sorted(rows, key=lambda row: (row[1], row[2], row[0]), reverse=True):
        print('  ' + ROW % (name, f'{date}T{clock}Z', size))
    return 0 if code == 0 and rows else 1
