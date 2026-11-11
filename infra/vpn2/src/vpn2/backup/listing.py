"""Листинг бэкапов: локальные архивы и S3."""
import os
import re

from vpn2.backup import archive, env

_TS = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z')
_S3_LINE = re.compile(r'anysda-vpn2-.*\.tar\.gz\.age$')
_ROW = '%-50s %-22s %12s'


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
                found.append((st.st_mtime_ns, os.fsencode(entry.path), entry.name, st.st_size))
        except OSError:
            continue
    rows = []
    for _, _, name, size in sorted(found, reverse=True):
        match = _TS.search(name)
        rows.append((name, match.group(0) if match else '?', size))
    return rows


def _s3_rows(text):
    """Строки `aws s3 ls` (дата время размер имя), свежие первыми, в «  имя метка размер»."""
    lines = sorted((line for line in text.split('\n') if _S3_LINE.search(line)),
                   key=lambda line: (line.split()[:2], line), reverse=True)
    rows = []
    for line in lines:
        f = line.split() + ['', '', '', '']
        rows.append('  ' + _ROW % (f[3], f[0] + 'T' + f[1] + 'Z', f[2]))
    return rows


def render(settings):
    """Печать листинга; код возврата: 0, а для s3 - 1, если aws упал или архивов нет."""
    print(_ROW % ('Name', 'Timestamp (UTC)', 'Size'))
    print(_ROW % ('----', '---------------', '----'))
    for name, ts, size in local_rows(settings.local_dir):
        print(_ROW % (name, ts, human_size(size)))
    if settings.backend != 's3':
        return 0
    print(f'\n── S3: s3://{settings.s3_bucket}/ ──', flush=True)
    code, text, _why = env.aws(settings, 'ls', f's3://{settings.s3_bucket}/', capture=True)
    rows = _s3_rows(text)
    print('\n'.join(rows), end='\n' if rows else '')
    return 0 if code == 0 and rows else 1
