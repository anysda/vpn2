import io
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vpn2.backup import env as benv
from vpn2.backup import listing
from vpn2.cli import backup_list

NEEDS_BASH = pytest.mark.skipif(
    shutil.which('bash') is None or shutil.which('numfmt') is None, reason='нужны bash и numfmt')
SCRIPT = Path(__file__).resolve().parents[2] / 'lib' / 'anysda-backup-list.sh'
MIB = 1024 ** 2
GIB = 1024 ** 3
# ожидания получены запуском numfmt --to=iec на тех же значениях
HUMAN = [
    (0, '0'), (1, '1'), (1023, '1023'), (1024, '1.0K'), (1025, '1.1K'), (1536, '1.5K'),
    (10240, '10K'), (10241, '11K'), (1048575, '1.0M'), (MIB, '1.0M'), (10 * MIB, '10M'),
    (999999, '977K'), (1000000000, '954M'), (GIB, '1.0G'), (GIB + GIB // 2, '1.5G'),
    (99999999999, '94G'),
]
S3_LINES = (
    '2026-05-22 02:00:01   1234567 anysda-vpn2-2026-05-22T020000Z.tar.gz.age\n'
    '2026-05-23 02:00:05     99 other.txt\n'
    '2026-05-23 02:00:07 7654321 anysda-vpn2-2026-05-23T020000Z.tar.gz.age\n'
)


@pytest.mark.parametrize(('n', 'text'), HUMAN)
def test_human_size_values(n, text):
    assert listing.human_size(n) == text


def make(path, name, size, mtime):
    path.mkdir(exist_ok=True)
    f = path / name
    f.write_bytes(b'x' * size)
    os.utime(f, (mtime, mtime))
    return f


def test_local_rows_order_filter_and_ts(tmp_path):
    make(tmp_path, 'anysda-vpn2-2026-05-01T000000Z.tar.gz.age', 0, 1000)
    make(tmp_path, 'anysda-vpn2-2026-05-03T000000Z.tar.gz.age', 1536, 3000)
    make(tmp_path, 'anysda-vpn2-noname.tar.gz.age', 5, 2000)
    make(tmp_path, 'readme.txt', 5, 4000)
    make(tmp_path, 'anysda-vpn2-2026-05-04T000000Z.tar.gz', 5, 5000)
    (tmp_path / 'anysda-vpn2-dir.tar.gz.age').mkdir()
    assert listing.local_rows(tmp_path) == [
        ('anysda-vpn2-2026-05-03T000000Z.tar.gz.age', '2026-05-03T000000Z', 1536),
        ('anysda-vpn2-noname.tar.gz.age', '?', 5),
        ('anysda-vpn2-2026-05-01T000000Z.tar.gz.age', '2026-05-01T000000Z', 0),
    ]
    assert listing.local_rows(tmp_path / 'nope') == []


def fake_run(text, code):
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(stdout=text.encode(), returncode=code)
    run.calls = calls
    return run


def s3_settings(tmp_path):
    return benv.Settings(backend='s3', local_dir=str(tmp_path / 'none'), s3_endpoint='https://e.example',
                         s3_bucket='bk', s3_region='eu-1', s3_access_key='ak', s3_secret_key='sk')


def test_render_s3_args_env_and_sort(tmp_path):
    run = fake_run(S3_LINES, 0)
    out = io.StringIO()
    rc = listing.render(s3_settings(tmp_path), run=run, out=out, environ={'PATH': '/x'})
    args, kw = run.calls[0]
    assert args == ['aws', '--endpoint-url', 'https://e.example', 's3', 'ls', 's3://bk/']
    assert kw['env'] == {'PATH': '/x', 'AWS_DEFAULT_REGION': 'eu-1',
                         'AWS_ACCESS_KEY_ID': 'ak', 'AWS_SECRET_ACCESS_KEY': 'sk'}
    lines = out.getvalue().split('\n')
    assert lines[2:4] == ['', '── S3: s3://bk/ ──']
    assert lines[4].split() == ['anysda-vpn2-2026-05-23T020000Z.tar.gz.age', '2026-05-23T02:00:07Z', '7654321']
    assert rc == 0


def test_render_s3_failures(tmp_path):
    assert listing.render(s3_settings(tmp_path), run=fake_run(S3_LINES, 1), out=io.StringIO()) == 1
    assert listing.render(s3_settings(tmp_path), run=fake_run('', 0), out=io.StringIO()) == 1

    def missing(*_a, **_k):
        raise FileNotFoundError('aws')
    out = io.StringIO()
    assert listing.render(s3_settings(tmp_path), run=missing, out=out) == 1
    assert out.getvalue().endswith('── S3: s3://bk/ ──\n')


def test_cli_errors_and_ok(tmp_path, capsys):
    assert backup_list.main(['--env-file', str(tmp_path / 'no.env')]) == 2
    assert 'не доступен' in capsys.readouterr().err
    bad = tmp_path / 'bad.env'
    bad.write_text('X=1\n')
    assert backup_list.main(['--env-file', str(bad)]) == 1
    assert 'BACKUP_BACKEND' in capsys.readouterr().err
    good = tmp_path / 'good.env'
    good.write_text(f'BACKUP_BACKEND=local\nBACKUP_LOCAL_DIR={tmp_path / "d"}\n')
    assert backup_list.main(['--env-file', str(good)]) == 0
    assert capsys.readouterr().out.startswith('Name ')


# удалить вместе с bash-телом
def bash_vs_python(tmp_path, lines, aws_code=None):
    env_file = tmp_path / 'backup.env'
    env_file.write_text(''.join(f'{k}={v}\n' for k, v in lines.items()))
    text = SCRIPT.read_text().replace('/etc/anysda/backup.env', str(env_file))
    assert text != SCRIPT.read_text()
    script = tmp_path / 'list.sh'
    script.write_text(text)
    environ = {'PATH': os.environ['PATH'], 'TZ': 'Pacific/Kiritimati', 'LC_ALL': 'C'}
    if aws_code is not None:
        stub = tmp_path / 'bin'
        stub.mkdir()
        (stub / 'aws').write_text(f"#!/bin/sh\nprintf '%s' '{S3_LINES}'\nexit {aws_code}\n")
        (stub / 'aws').chmod(0o755)
        environ['PATH'] = f'{stub}:{environ["PATH"]}'
    ref = subprocess.run(['bash', str(script)], env=environ, capture_output=True)
    out = io.StringIO()
    values = benv.settings(benv.read_env(env_file), str(env_file))
    rc = listing.render(values, out=out, environ=environ)
    assert out.getvalue().encode() == ref.stdout
    assert rc == ref.returncode
    return rc


@NEEDS_BASH
def test_bash_empty_missing_and_other_files(tmp_path):
    d = tmp_path / 'arch'
    d.mkdir()
    cfg = {'BACKUP_BACKEND': 'local', 'BACKUP_LOCAL_DIR': str(d)}
    assert bash_vs_python(tmp_path, cfg) == 0
    make(d, 'foreign.txt', 3, 100)
    make(d, 'anysda-vpn2-x.tar.gz', 3, 100)
    assert bash_vs_python(tmp_path, cfg) == 0
    cfg['BACKUP_LOCAL_DIR'] = str(tmp_path / 'missing')
    assert bash_vs_python(tmp_path, cfg) == 0


@NEEDS_BASH
def test_bash_three_archives_long_name(tmp_path):
    d = tmp_path / 'arch'
    make(d, 'anysda-vpn2-2026-05-01T000000Z.tar.gz.age', 0, 1000)
    make(d, 'anysda-vpn2-2026-05-03T000000Z.tar.gz.age', 1536, 3000)
    make(d, 'anysda-vpn2-2026-05-02T000000Z.tar.gz.age', 5 * MIB, 2000)
    make(d, 'anysda-vpn2-' + 'long' * 15 + '-2026-05-04T000000Z.tar.gz.age', 10, 500)
    make(d, 'anysda-vpn2-tie-b.tar.gz.age', 1, 7000)
    make(d, 'anysda-vpn2-tie-a.tar.gz.age', 2, 7000)
    cfg = {'BACKUP_BACKEND': 'local', 'BACKUP_LOCAL_DIR': str(d)}
    assert bash_vs_python(tmp_path, cfg) == 0


@NEEDS_BASH
@pytest.mark.parametrize('aws_code', [0, 1])
def test_bash_s3_with_stub(tmp_path, aws_code):
    d = tmp_path / 'arch'
    make(d, 'anysda-vpn2-2026-05-01T000000Z.tar.gz.age', 1536, 1000)
    cfg = {'BACKUP_BACKEND': 's3', 'BACKUP_LOCAL_DIR': str(d), 'BACKUP_S3_ENDPOINT': 'https://e.example',
           'BACKUP_S3_BUCKET': 'bk', 'ANYSDA_S3_ACCESS_KEY': 'ak', 'ANYSDA_S3_SECRET_KEY': 'sk'}
    assert bash_vs_python(tmp_path, cfg, aws_code) == aws_code


@NEEDS_BASH
def test_bash_s3_no_aws_and_no_dir(tmp_path):
    cfg = {'BACKUP_BACKEND': 's3', 'BACKUP_LOCAL_DIR': str(tmp_path / 'none'),
           'BACKUP_S3_ENDPOINT': 'https://e.example', 'BACKUP_S3_BUCKET': 'bk',
           'ANYSDA_S3_ACCESS_KEY': 'ak', 'ANYSDA_S3_SECRET_KEY': 'sk', 'BACKUP_S3_REGION': 'eu-2'}
    empty = tmp_path / 'emptybin'
    empty.mkdir()
    env_path = os.environ['PATH']
    os.environ['PATH'] = str(empty) + ':' + env_path  # aws в системе может быть
    try:
        if shutil.which('aws', path=str(empty)) is None and shutil.which('aws') is None:
            assert bash_vs_python(tmp_path, cfg) == 1
    finally:
        os.environ['PATH'] = env_path


@NEEDS_BASH
def test_bash_human_size_matches_numfmt():
    vals = [0, 1, 1023, 1024, 1025, 1536, 10239, 10240, 10241, 1048575, MIB, 10 * MIB, GIB - 1, GIB, GIB * 3 // 2, 10 * GIB - 1]
    ref = subprocess.run(['numfmt', '--to=iec', *map(str, vals)], capture_output=True, text=True).stdout.split()
    assert [listing.human_size(v) for v in vals] == ref
