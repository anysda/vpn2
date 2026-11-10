import io
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vpn2.backup import env as benv
from vpn2.backup import select
from vpn2.backup.errors import RestoreError

SCRIPT = Path(__file__).resolve().parent / 'fixtures' / 'backup' / 'anysda-restore.sh'
NEEDS_BASH = pytest.mark.skipif(shutil.which('bash') is None, reason='нужен bash')
NOW = 1_780_000_000


def name(ts):
    return f'anysda-vpn2-{ts}.tar.gz.age'


def conf(tmp_path, backend='local'):
    env = {'BACKUP_BACKEND': backend, 'BACKUP_LOCAL_DIR': str(tmp_path / 'bk')}
    if backend == 's3':
        env.update(BACKUP_S3_ENDPOINT='https://s3.test', BACKUP_S3_BUCKET='bkt',
                   ANYSDA_S3_ACCESS_KEY='ak', ANYSDA_S3_SECRET_KEY='sk')
    return benv.settings(env)


def put(settings, *names):
    Path(settings.local_dir).mkdir(parents=True, exist_ok=True)
    for n in names:
        (Path(settings.local_dir) / n).write_bytes(b'x')


def aws(text, code=0):
    calls = []

    def run(args, **_kw):
        calls.append((args, _kw))
        return SimpleNamespace(stdout=text.encode(), returncode=code)
    run.calls = calls
    return run


def latest(settings, run=None, now=NOW):
    err = io.StringIO()
    try:
        res = select.resolve_latest(settings, run=run or aws(''), now=now, err=err)
    except RestoreError as e:
        return ('err', e.code, e.message), err.getvalue()
    return res, err.getvalue()


# ожидания получены разбором скрипта: порядок и тексты строк 40-51
@pytest.mark.parametrize(('argv', 'want'), [
    ([], select.Args('latest')),
    (['x.age'], select.Args('x.age')),
    (['--dry-run'], select.Args('latest', dry_run=True)),
    (['a b', '-f', '--allow-incompatible', '--dry-run'], select.Args('a b', True, True, True)),
    (['--force', '--force'], select.Args('latest', force=True)),
    (['latest', '--force'], select.Args('latest', force=True)),
])
def test_parse_args_forms(argv, want):
    assert select.parse_args(argv) == want


@pytest.mark.parametrize(('argv', 'text'), [
    (['--bogus'], 'anysda-restore: неизвестный аргумент: --bogus'),
    (['a', 'b'], 'anysda-restore: неизвестный аргумент: b'),
    (['--force', 'a'], 'anysda-restore: неизвестный аргумент: a'),
    ([''], 'anysda-restore: неизвестный аргумент: '),
    (['a', '-x', '--force'], 'anysda-restore: неизвестный аргумент: -x'),
])
def test_parse_args_errors(argv, text):
    with pytest.raises(RestoreError) as exc:
        select.parse_args(argv)
    assert (exc.value.code, exc.value.message) == (2, text)


@pytest.mark.parametrize(('given', 'want'), [
    (name('2026-05-22T020000Z'), '2026-05-22T020000Z'),
    (name('2026-12-31T235959Z'), '2026-12-31T235959Z'),
    ('anysda-vpn2-2026-05-22T020000Z.tar.gz', None),
    ('x' + name('2026-05-22T020000Z'), None),
    (name('2026-05-22T020000Z') + '\n', None),
    (name('2026-5-22T020000Z'), None),
    ('', None),
])
def test_archive_ts(given, want):
    assert select.archive_ts(given) == want


@pytest.mark.parametrize(('ts', 'epoch'), [
    ('1970-01-01T000000Z', 0),
    ('2026-05-22T020000Z', 1779415200),
    ('2024-02-29T235959Z', 1709251199),
    ('2025-12-31T235959Z', 1767225599),
    ('2026-01-01T000000Z', 1767225600),
])
def test_ts_epoch(ts, epoch):
    assert select.ts_epoch(ts) == epoch


# значения - вывод age_text из скрипта
@pytest.mark.parametrize(('sec', 'text'), [
    (0, '0 мин'), (59, '0 мин'), (60, '1 мин'), (3599, '59 мин'), (3600, '1 ч 0 мин'),
    (86399, '23 ч 59 мин'), (86400, '1 д 0 ч'), (172800, '2 д 0 ч'), (190000, '2 д 4 ч'),
    (-1, '0 мин'), (-61, '-1 мин'),
])
def test_age_text(sec, text):
    assert select.age_text(sec) == text


def test_dry_run_text():
    assert select.dry_run_text('a.age', 'S3') == (
        'anysda-restore: dry-run — восстановил бы a.age (S3), ничего не изменено')


def test_latest_local_picks_newest_and_ignores_foreign(tmp_path):
    s = conf(tmp_path)
    put(s, name('2026-05-22T020000Z'), name('2026-05-23T020000Z'), 'notes.txt',
        'anysda-vpn2-bad.tar.gz.age', name('2026-05-24T020000Z') + '.part')
    (Path(s.local_dir) / name('2026-06-01T020000Z')).mkdir()
    res, err = latest(s, now=select.ts_epoch('2026-05-23T020000Z') + 3 * 3600 + 5)
    assert res == (name('2026-05-23T020000Z'), 'local')
    assert err == ('anysda-restore: latest = anysda-vpn2-2026-05-23T020000Z.tar.gz.age (local), '
                   'создан 2026-05-23T020000Z, 3 ч 0 мин назад\n')


def test_latest_nothing_anywhere(tmp_path):
    res, err = latest(conf(tmp_path))
    assert res == ('err', 3, f"anysda-restore: ни одного backup'а ни в {tmp_path / 'bk'}, ни в S3")
    assert err == ''


def test_latest_s3_newer_reports_local_gap(tmp_path):
    s = conf(tmp_path, 's3')
    put(s, name('2026-05-22T020000Z'))
    run = aws('2026-05-23 02:00:07 99 ' + name('2026-05-23T020000Z') + '\nPRE dir/\n')
    res, err = latest(s, run, now=select.ts_epoch('2026-05-23T020000Z') + 100)
    assert res == (name('2026-05-23T020000Z'), 'S3')
    assert err.splitlines()[1] == ('anysda-restore:   самый свежий local: ' + name('2026-05-22T020000Z')
                                   + ' — старше выбранного на 1 д 0 ч')
    args, kw = run.calls[0]
    assert args == ['aws', '--endpoint-url', 'https://s3.test', 's3', 'ls', 's3://bkt/']
    assert (kw['env']['AWS_ACCESS_KEY_ID'], kw['env']['AWS_SECRET_ACCESS_KEY']) == ('ak', 'sk')


def test_latest_same_archive_local_and_s3_prefers_local(tmp_path):
    s = conf(tmp_path, 's3')
    n = name('2026-05-22T020000Z')
    put(s, n)
    res, err = latest(s, aws(f'2026-05-22 02:00:07 9 {n}\n'))
    assert res == (n, 'local')
    assert err.count('\n') == 1


def test_latest_s3_older_than_local_reports_s3_gap(tmp_path):
    s = conf(tmp_path, 's3')
    put(s, name('2026-05-23T020000Z'))
    res, err = latest(s, aws(f'x 1 {name("2026-05-22T020000Z")}\n'))
    assert res[1] == 'local'
    assert 'самый свежий S3: ' + name('2026-05-22T020000Z') + ' — старше выбранного на 1 д 0 ч' in err


def test_latest_s3_down_warns_with_last_three_lines(tmp_path):
    s = conf(tmp_path, 's3')
    put(s, name('2026-05-22T020000Z'))
    res, err = latest(s, aws('l1\nl2\nl3\nl4\n', 255))
    assert res[1] == 'local'
    assert err.startswith('anysda-restore: WARN: S3 недоступен — выбираю только среди локальных архивов, '
                          'в облаке может лежать более свежий:\n  l2\n  l3\n  l4\nanysda-restore: latest')


def test_latest_s3_down_and_no_local_is_error_3(tmp_path):
    res, err = latest(conf(tmp_path, 's3'), aws('', 1))
    assert res[:2] == ('err', 3)
    assert err.endswith(':\n  \n')


def test_latest_aws_missing_counts_as_unavailable(tmp_path):
    def run(*_a, **_k):
        raise FileNotFoundError('aws')
    s = conf(tmp_path, 's3')
    put(s, name('2026-05-22T020000Z'))
    res, err = latest(s, run)
    assert res[1] == 'local' and 'WARN: S3 недоступен' in err


def test_latest_non_ascii_and_space_dir(tmp_path):
    s = benv.settings({'BACKUP_BACKEND': 'local', 'BACKUP_LOCAL_DIR': str(tmp_path / 'бэкап dir')})
    put(s, name('2026-05-22T020000Z'))
    assert latest(s)[0] == (name('2026-05-22T020000Z'), 'local')


def test_s3_pull_copies_and_sets_mode(tmp_path):
    s = conf(tmp_path, 's3')
    dest = tmp_path / 'new' / 'bk'

    def run(args, **_kw):
        Path(args[6]).write_bytes(b'data')  # целевой путь после s3://...
        return SimpleNamespace(returncode=0)
    assert select.s3_pull(s, 'a.age', str(dest), run=run)
    assert (dest / 'a.age').read_bytes() == b'data'
    assert (dest / 'a.age').stat().st_mode & 0o777 == 0o600


def test_s3_pull_failures_return_false(tmp_path):
    s = conf(tmp_path, 's3')
    calls = []

    def run(args, **_kw):
        calls.append(args)
        return SimpleNamespace(returncode=1)
    assert not select.s3_pull(s, 'a.age', str(tmp_path / 'bk'), run=run)
    assert calls[0] == ['aws', '--endpoint-url', 'https://s3.test', 's3', 'cp', 's3://bkt/a.age',
                        str(tmp_path / 'bk' / 'a.age'), '--only-show-errors']

    def gone(*_a, **_k):
        raise FileNotFoundError('aws')
    assert not select.s3_pull(s, 'a.age', str(tmp_path / 'bk'), run=gone)
    # aws ничего не записал: chmod падает - тоже отказ
    assert not select.s3_pull(s, 'a.age', str(tmp_path / 'bk'), run=lambda *_a, **_k: SimpleNamespace(returncode=0))


# --- прямая сверка с bash; удалить вместе с bash-телом ---
def bash_funcs():
    text = SCRIPT.read_text()
    first = text[text.index('s3() {'):text.index('s3_pull() {')]
    second = text[text.index('archive_ts() {'):text.index('if [[ "$ARCHIVE_REQ" == "latest" ]]')]
    return first + second


def bash_run(tmp_path, call, backend='local', aws_text='', aws_code=0, now=NOW):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / 'aws.out').write_text(aws_text)
    fake = tmp_path / 'fakebin' / 'aws'
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(f'#!/bin/sh\ncat "{tmp_path}/aws.out"\nexit {aws_code}\n')
    fake.chmod(0o755)
    (tmp_path / 'funcs.sh').write_text(bash_funcs())
    prog = (f'set -euo pipefail\nsource "{tmp_path}/funcs.sh"\n'
            f'date() {{ if [[ "$*" == "-u +%s" ]]; then echo {now}; else command date "$@"; fi; }}\n' + call)
    env = {'PATH': f'{tmp_path}/fakebin:' + os.environ['PATH'], 'BACKUP_BACKEND': backend,
           'BACKUP_LOCAL_DIR': str(tmp_path / 'bk'), 'BACKUP_S3_ENDPOINT': 'https://s3.test',
           'BACKUP_S3_BUCKET': 'bkt', 'ANYSDA_S3_ACCESS_KEY': 'ak', 'ANYSDA_S3_SECRET_KEY': 'sk'}
    done = subprocess.run(['bash', '-c', prog], env=env, capture_output=True, text=True, check=False)
    return done.stdout, done.stderr, done.returncode


@NEEDS_BASH
@pytest.mark.parametrize('sec', [0, 59, 60, 3599, 3600, 86399, 86400, 172800, 400000])
def test_bash_age_text(tmp_path, sec):
    out, _, code = bash_run(tmp_path, f'age_text {sec}')
    assert (out.strip(), code) == (select.age_text(sec), 0)


@NEEDS_BASH
@pytest.mark.parametrize('ts', ['2025-12-31T235959Z', '2026-01-01T000000Z', '2024-02-29T120000Z',
                                '2026-05-22T020000Z', '1999-12-31T235959Z'])
def test_bash_ts_epoch_and_ts(tmp_path, ts):
    out, _, _ = bash_run(tmp_path, f'ts_epoch {ts}; archive_ts {name(ts)}')
    assert out.split() == [str(select.ts_epoch(ts)), select.archive_ts(name(ts))]


@NEEDS_BASH
@pytest.mark.parametrize('case', ['empty', 'two_local', 's3_newer', 's3_down', 'same_both', 'noise_only'])
def test_bash_resolve_latest(tmp_path, case):
    local = {'empty': [], 'two_local': ['2025-12-31T235959Z', '2026-01-01T000000Z'],
             's3_newer': ['2026-05-22T020000Z'], 's3_down': ['2026-05-22T020000Z'],
             'same_both': ['2026-05-22T020000Z'], 'noise_only': ['junk.txt']}[case]
    s3 = {'s3_newer': '2026-05-23 02:00:07 9 ' + name('2026-05-23T020000Z') + '\n',
          'same_both': '2026-05-22 02:00:07 9 ' + name('2026-05-22T020000Z') + '\n',
          's3_down': 'boom\nline2\n'}.get(case, '')
    backend = 'local' if case in ('empty', 'two_local', 'noise_only') else 's3'
    sets = conf(tmp_path / 'py', backend)
    put(sets, *[name(t) if t.startswith('20') else t for t in local])
    for n in os.listdir(sets.local_dir):  # те же файлы для bash
        (tmp_path / 'bk').mkdir(exist_ok=True)
        (tmp_path / 'bk' / n).write_bytes(b'x')
    code_in = 1 if case == 's3_down' else 0
    out, err, code = bash_run(tmp_path, 'resolve_latest; echo "$ARCHIVE_NAME $ARCHIVE_FROM"',
                              backend, s3, code_in)
    got, py_err = latest(sets, aws(s3, code_in))
    py_err = py_err.replace(str(tmp_path / 'py'), str(tmp_path))
    if isinstance(got[0], str) and got[0] == 'err':
        assert (code, err) == (got[1], got[2].replace(str(tmp_path / 'py'), str(tmp_path)) + '\n')
    else:
        assert (code, out.strip(), err) == (0, ' '.join(got), py_err)


def test_args_flags_readable():
    a = select.parse_args(['--dry-run', '--allow-incompatible'])
    assert (a.name, a.force, a.dry_run, a.allow_incompatible) == ('latest', False, True, True)
