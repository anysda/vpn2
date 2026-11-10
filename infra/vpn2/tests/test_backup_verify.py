import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vpn2.backup import bundle, verify
from vpn2.backup.errors import RestoreError

SCRIPT = Path(__file__).resolve().parents[2] / 'lib' / 'anysda-restore.sh'
NEEDS_PY = pytest.mark.skipif(shutil.which('python3') is None, reason='нужен python3')
ARCHIVE = 'anysda-vpn2-2026-05-22T020000Z.tar.gz.age'


def make_bundle(tmp_path, extra=None):
    b = tmp_path / 'anysda-vpn2-2026-05-22T020000Z'
    (b / 'pki').mkdir(parents=True)
    (b / 'config.yaml').write_text('a: 1\n')
    (b / 'pki' / 'ca.crt').write_text('ca')
    (b / 'pki' / 'ню file.txt').write_text('x')
    m = bundle.manifest(b, version='1.2.3', git_commit='abc', schema_version=3, timestamp='2026-05-22T020000Z')
    for name, text in (extra or {}).items():
        (b / name).write_text(text)
    bundle.write_manifest(b, m)
    return b, m


def check(b, **_kw):
    out, err = io.StringIO(), io.StringIO()
    try:
        res = verify.check_manifest(str(b), out=out, err=err, **_kw)
    except RestoreError as e:
        return ('err', e.code, e.message), out.getvalue(), err.getvalue()
    return res, out.getvalue(), err.getvalue()


def test_manifest_ok(tmp_path):
    b, m = make_bundle(tmp_path)
    res, out, err = check(b, archive_name=ARCHIVE)
    assert res == m and err == ''
    assert out == 'manifest verified: 2 components, archive=2026-05-22T020000Z app_v=1.2.3 git=abc\n'


def test_manifest_extra_file_is_fine(tmp_path):
    b, _ = make_bundle(tmp_path)
    (b / 'extra.txt').write_text('lishnee')
    assert isinstance(check(b)[0], dict)


def test_manifest_corrupted_file(tmp_path):
    b, m = make_bundle(tmp_path)
    (b / 'config.yaml').write_text('a: 2\n')
    res, out, _ = check(b)
    assert out == ''
    assert res == ('err', 5, 'manifest verification FAILED:\n  config.yaml: expected '
                   f"{m['components']['config.yaml']}, got {bundle.sha_file(b / 'config.yaml')}")


def test_manifest_missing_component_and_changed_dir(tmp_path):
    b, m = make_bundle(tmp_path)
    (b / 'config.yaml').unlink()
    (b / 'pki' / 'ca.crt').write_text('other')
    res, _, _ = check(b)
    assert res[:2] == ('err', 5)
    assert res[2].splitlines()[:2] == ['manifest verification FAILED:', '  config.yaml: missing']
    assert res[2].splitlines()[2] == f"  pki: expected {m['components']['pki']}, got {bundle.sha_dir(b / 'pki')}"


def test_manifest_absent_and_broken(tmp_path):
    b, _ = make_bundle(tmp_path)
    (b / 'manifest.json').write_text('{oops')
    res, _, _ = check(b)
    assert res[:2] == ('err', 5) and 'не разобран' in res[2]
    (b / 'manifest.json').write_text('[]')
    assert check(b)[0][:2] == ('err', 5)
    (b / 'manifest.json').write_text('{}')
    assert check(b)[0][:2] == ('err', 5)
    (b / 'manifest.json').unlink()
    assert check(b)[0] == ('err', 5, 'anysda-restore: manifest.json отсутствует — corrupted архив')


def test_manifest_name_mismatch_warns_but_passes(tmp_path):
    b, _ = make_bundle(tmp_path)
    res, out, err = check(b, archive_name='anysda-vpn2-2026-05-23T020000Z.tar.gz.age')
    assert isinstance(res, dict) and out.startswith('manifest verified')
    assert err == 'anysda-restore: WARN: имя архива не совпадает с manifest.timestamp=2026-05-22T020000Z\n'


def test_manifest_repeat_gives_same(tmp_path):
    b, _ = make_bundle(tmp_path)
    assert check(b) == check(b)


# --- прямая сверка с bash; удалить вместе с bash-телом ---
def heredoc():
    text = SCRIPT.read_text()
    start = text.index("<<'PY' || exit 5\n") + len("<<'PY' || exit 5\n")
    return text[start:text.index('\nPY\n', start) + 1]


def old_manifest(tmp_path, b, archive):
    prog = tmp_path / 'old.py'
    prog.write_text(heredoc())
    done = subprocess.run([sys.executable, str(prog), str(b), archive], capture_output=True, text=True, check=False)
    return done.stdout, done.stderr, done.returncode


@NEEDS_PY
@pytest.mark.parametrize('case', ['ok', 'corrupt', 'missing', 'extra', 'name'])
def test_old_heredoc_matches(tmp_path, case):
    b, _ = make_bundle(tmp_path)
    archive = ARCHIVE
    if case == 'corrupt':
        (b / 'pki' / 'ca.crt').write_text('zzz')
    elif case == 'missing':
        (b / 'config.yaml').unlink()
    elif case == 'extra':
        (b / 'extra').write_text('1')
    else:
        archive = 'other.tar.gz.age' if case == 'name' else archive
    res, out, err = check(b, archive_name=archive)
    old_out, old_err, code = old_manifest(tmp_path, b, archive)
    assert old_out == out
    if isinstance(res, tuple):
        assert (code, old_err) == (1, err + res[2] + '\n')
    else:
        assert (code, old_err) == (0, err)


# --- совместимость ---
SQL = {'0000_a': 'CREATE TABLE a(x);', '0001_b': 'CREATE TABLE b(y);'}


def journal(folder, sqls=SQL):
    (folder / 'meta').mkdir(parents=True)
    entries = []
    for i, (tag, sql) in enumerate(sqls.items()):
        (folder / f'{tag}.sql').write_text(sql)
        entries.append({'tag': tag, 'when': 1000 + i})
    (folder / 'meta' / '_journal.json').write_text(json.dumps({'entries': entries}))
    return folder


def database(path, applied=2, sqls=SQL, tables=()):
    db = sqlite3.connect(path)
    if applied is not None:
        db.execute('CREATE TABLE __drizzle_migrations (id INTEGER PRIMARY KEY, hash TEXT, created_at NUMERIC)')
        for i, sql in list(enumerate(sqls.values()))[:applied]:
            db.execute('INSERT INTO __drizzle_migrations (hash, created_at) VALUES (?, ?)',
                       (hashlib.sha256(sql.encode()).hexdigest(), 1000 + i))
    for t in tables:
        db.execute(f'CREATE TABLE {t}(x)')
    db.commit()
    db.close()


def compat(tmp_path, m, *, docker=None, source=None, allow=False):
    b, w = tmp_path / 'b', tmp_path / 'w'
    b.mkdir(exist_ok=True)
    w.mkdir(exist_ok=True)
    out, err = io.StringIO(), io.StringIO()

    def run(args, **_kw):
        assert args[:3] == ['docker', 'cp', 'anysda-vpn2:/app/server/database/migrations']
        if docker is None:
            return SimpleNamespace(returncode=1)
        shutil.copytree(docker, args[3], dirs_exist_ok=True)
        return SimpleNamespace(returncode=0)
    try:
        verify.check_compat(m, str(b), str(w), source_root=str(source or tmp_path / 'nosrc'),
                            run=run, allow=allow, out=out, err=err)
    except RestoreError as e:
        return ('err', e.code, e.message), out.getvalue(), err.getvalue()
    return None, out.getvalue(), err.getvalue()


def test_compat_no_db_only_spec(tmp_path):
    assert compat(tmp_path, {'spec_version': 1}) == (None, '', '')
    assert compat(tmp_path, {}) == (None, '', '')


def test_compat_spec_mismatch(tmp_path):
    res, _, _ = compat(tmp_path, {'spec_version': 2})
    assert res == ('err', 9, 'anysda-restore: ABORT — формат архива spec_version=2, этот restore понимает '
                   'только 1; данные не изменены (обход: --allow-incompatible)')
    _, _, err = compat(tmp_path, {'spec_version': 2}, allow=True)
    assert err == ('anysda-restore: WARN: формат архива spec_version=2, этот restore понимает только 1 — '
                   'продолжаю по --allow-incompatible\n')
    assert compat(tmp_path, {'spec_version': '1'})[0] is None
    assert compat(tmp_path, {'spec_version': 1.0})[0][1] == 9


def test_compat_schema_from_docker_equal(tmp_path):
    (tmp_path / 'b').mkdir()
    database(tmp_path / 'b' / 'db.sqlite')
    res, out, _ = compat(tmp_path, {}, docker=journal(tmp_path / 'j'))
    assert (res, out) == (None, 'anysda-restore: схема БД архива: 2 из 2 миграций текущей версии\n')


def test_compat_schema_behind_and_empty_db(tmp_path):
    (tmp_path / 'b').mkdir()
    database(tmp_path / 'b' / 'db.sqlite', applied=1)
    j = journal(tmp_path / 'j')
    out = compat(tmp_path, {}, docker=j)[1]
    assert out == 'anysda-restore: схема БД архива: 1 из 2 миграций текущей версии, 1 догонит стадия 30\n'
    (tmp_path / 'b' / 'db.sqlite').unlink()
    database(tmp_path / 'b' / 'db.sqlite', applied=None)
    assert compat(tmp_path, {}, docker=j)[1].startswith('anysda-restore: схема БД архива: 0 из 2')


def test_compat_schema_newer_diverged_nohistory(tmp_path):
    (tmp_path / 'b').mkdir()
    j = journal(tmp_path / 'j', {'0000_a': SQL['0000_a']})
    database(tmp_path / 'b' / 'db.sqlite', applied=2)
    res = compat(tmp_path, {}, docker=j)[0]
    assert res == ('err', 9, 'anysda-restore: ABORT — схема БД архива новее текущей версии: 2 миграций против 1; '
                   'данные не изменены (обход: --allow-incompatible)')
    (tmp_path / 'b' / 'db.sqlite').unlink()
    database(tmp_path / 'b' / 'db.sqlite', applied=2, sqls={'0000_a': 'CREATE TABLE z(q);', '0001_b': SQL['0001_b']})
    assert 'расходится с текущей версией на 0000_a;' in compat(tmp_path, {}, docker=journal(tmp_path / 'j2'))[0][2]
    (tmp_path / 'b' / 'db.sqlite').unlink()
    database(tmp_path / 'b' / 'db.sqlite', applied=None, tables=['t'])
    assert ('в базе архива нет истории миграций (__drizzle_migrations);'
            in compat(tmp_path, {}, docker=journal(tmp_path / 'j3'))[0][2])


def test_compat_allow_downgrades_to_warning(tmp_path):
    (tmp_path / 'b').mkdir()
    database(tmp_path / 'b' / 'db.sqlite', applied=None, tables=['t'])
    res, out, err = compat(tmp_path, {}, docker=journal(tmp_path / 'j'), allow=True)
    assert res is None and out == ''
    assert err == ('anysda-restore: WARN: в базе архива нет истории миграций (__drizzle_migrations) — '
                   'продолжаю по --allow-incompatible\n')


def test_compat_journal_from_sources_when_docker_fails(tmp_path):
    (tmp_path / 'b').mkdir()
    database(tmp_path / 'b' / 'db.sqlite')
    src = tmp_path / 'src'
    journal(src / 'app' / 'server' / 'database' / 'migrations')
    res, out, _ = compat(tmp_path, {}, source=src)
    assert (res, out) == (None, 'anysda-restore: схема БД архива: 2 из 2 миграций текущей версии\n')


def test_compat_docker_copy_without_journal_falls_back(tmp_path):
    (tmp_path / 'b').mkdir()
    database(tmp_path / 'b' / 'db.sqlite')
    (tmp_path / 'empty').mkdir()
    src = tmp_path / 'src'
    journal(src / 'app' / 'server' / 'database' / 'migrations')
    assert compat(tmp_path, {}, docker=tmp_path / 'empty', source=src)[0] is None


def test_compat_no_journal_anywhere(tmp_path):
    (tmp_path / 'b').mkdir()
    database(tmp_path / 'b' / 'db.sqlite')
    res, _, _ = compat(tmp_path, {}, source=tmp_path / 'nosrc')
    assert res == ('err', 9, 'anysda-restore: ABORT — не найден журнал миграций текущей версии (ни в контейнере '
                   f"anysda-vpn2, ни в {tmp_path / 'nosrc'}); данные не изменены (обход: --allow-incompatible)")
    _, out, err = compat(tmp_path, {}, source=tmp_path / 'nosrc', allow=True)
    assert out == '' and err.startswith('anysda-restore: WARN: не найден журнал')


def test_compat_docker_missing_uses_sources(tmp_path):
    (tmp_path / 'b').mkdir()
    database(tmp_path / 'b' / 'db.sqlite')
    src = tmp_path / 'src'
    journal(src / 'app' / 'server' / 'database' / 'migrations')

    def run(*_a, **_k):
        raise FileNotFoundError('docker')
    out = io.StringIO()
    verify.check_compat({}, str(tmp_path / 'b'), str(tmp_path), source_root=str(src), run=run, out=out)
    assert out.getvalue().startswith('anysda-restore: схема БД архива: 2 из 2')


def test_compat_broken_db_is_incompatible(tmp_path):
    (tmp_path / 'b').mkdir()
    (tmp_path / 'b' / 'db.sqlite').write_text('not a database')
    res = compat(tmp_path, {}, docker=journal(tmp_path / 'j'))[0]
    assert res[:2] == ('err', 9) and 'DatabaseError' in res[2]
