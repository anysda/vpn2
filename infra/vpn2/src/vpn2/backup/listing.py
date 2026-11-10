"""Листинг бэкапов (перенос infra/lib/anysda-backup-list.sh): локальные архивы и S3."""
import fnmatch
import os
import re
import subprocess
import sys

_MASK = 'anysda-vpn2-*.tar.gz.age'
_TS = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z')
_S3_LINE = re.compile(r'anysda-vpn2-.*\.tar\.gz\.age$')
_SORT_KEY = re.compile(r'^[ \t]*[^ \t]*[ \t]*[^ \t]*')
_ROW = '%-50s %-22s %12s\n'
_FIELDS = re.compile(r'[ \t]+')


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
    """[(имя, метка или '?', размер в байтах)] по mtime убыванию, как find | sort -nr."""
    found = []
    try:
        entries = list(os.scandir(local_dir))
    except OSError:
        return []
    for entry in entries:
        if not fnmatch.fnmatchcase(entry.name, _MASK):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        found.append((st.st_mtime_ns, os.fsencode(entry.path), entry.name, st.st_size))
    # sort -nr: при равном mtime последнее сравнение - вся строка, тоже в обратную сторону
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    rows = []
    for _, _, name, size in found:
        match = _TS.search(name)
        rows.append((name, match.group(0) if match else '?', size))
    return rows


def _s3_rows(text):
    """grep -E | sort -k1,2 -r | awk: строки aws s3 ls в «  имя метка размер»."""
    lines = [ln for ln in text.split('\n') if _S3_LINE.search(ln)]
    lines.sort(key=lambda ln: (_SORT_KEY.match(ln).group(0).encode(errors='surrogateescape'),
                               ln.encode(errors='surrogateescape')), reverse=True)
    out = []
    for ln in lines:
        f = [x for x in _FIELDS.split(ln) if x] + ['', '', '', '']
        out.append('  %-50s %-22s %12s\n' % (f[3], f[0] + 'T' + f[1] + 'Z', f[2]))
    return out, bool(lines)


def render(settings, *, run=subprocess.run, out=None, environ=None):
    """Печать листинга как скрипт; код возврата: 0, а для s3 - 1, если aws упал или строк нет (pipefail)."""
    out = sys.stdout if out is None else out
    out.write(_ROW % ('Name', 'Timestamp (UTC)', 'Size'))
    out.write(_ROW % ('----', '---------------', '----'))
    for name, ts, size in local_rows(settings.local_dir):
        out.write(_ROW % (name, ts, human_size(size)))
    if settings.backend != 's3':
        return 0
    out.write('\n')
    out.write(f'── S3: s3://{settings.s3_bucket}/ ──\n')
    env = dict(os.environ if environ is None else environ)
    env['AWS_DEFAULT_REGION'] = settings.s3_region
    env['AWS_ACCESS_KEY_ID'] = settings.s3_access_key
    env['AWS_SECRET_ACCESS_KEY'] = settings.s3_secret_key
    args = ['aws', '--endpoint-url', settings.s3_endpoint, 's3', 'ls', f's3://{settings.s3_bucket}/']
    try:
        done = run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, check=False)
        text, code = done.stdout.decode('utf-8', 'surrogateescape'), done.returncode
    except OSError:
        text, code = '', 127
    rows, matched = _s3_rows(text)
    out.writelines(rows)
    return 0 if code == 0 and matched else 1
