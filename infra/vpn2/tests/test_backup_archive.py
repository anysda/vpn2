import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vpn2.backup import archive
from vpn2.backup.archive import ArchiveError, decrypt, encrypt, pack, unpack, wipe, workdir

LIB = Path(__file__).resolve().parent / 'fixtures' / 'backup'
HAS_AGE = shutil.which('age') is not None and shutil.which('expect') is not None
NEEDS_AGE = pytest.mark.skipif(not HAS_AGE, reason='нужны age и expect')
NEEDS_TAR = pytest.mark.skipif(shutil.which('tar') is None, reason='нужен tar')
PASSWORDS = ['plain', 'a b', 'it\'s "q"', '$HOME `id` $(x)', 'пароль и ключ', '  edge  ']
FAKE_AGE = '''#!{python}
import json, os, sys, termios
args = sys.argv[1:]
tty = open('/dev/tty', 'r+b', buffering=0)
attrs = termios.tcgetattr(tty)
attrs[3] &= ~termios.ECHO
termios.tcsetattr(tty, termios.TCSANOW, attrs)
def ask(text):
    tty.write(text.encode())
    return tty.readline().rstrip(b'\\r\\n').decode()
mode = os.environ.get('FAKE_MODE', '')
if mode == 'hang':
    tty.write(b'Enter passphrase: ')
    sys.stdin.read()
store = os.environ['FAKE_STORE']
first = ask('Enter passphrase (leave empty): ')
if args[0] == '-p':
    second = ask('Confirm passphrase: ')
    ok = first == second and mode != 'fail'
    out = args[2]
    if ok and mode != 'empty':
        open(out, 'wb').write(b'ENC\\n' + first.encode() + b'\\n' + open(args[3], 'rb').read())
    elif ok:
        open(out, 'wb').close()
else:
    data = open(args[3], 'rb').read().split(b'\\n', 2)
    ok = data[1].decode() == first
    if ok:
        open(args[2], 'wb').write(data[2])
json.dump({{'argv': args, 'cmdline': open('/proc/self/cmdline', 'rb').read().decode().split(chr(0)),
           'environ': open('/proc/self/environ', 'rb').read().decode(), 'ppid_cmd':
           open('/proc/%d/cmdline' % os.getppid(), 'rb').read().decode()}}, open(store, 'w'))
sys.exit(0 if ok else 1)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Поддельный age в tmp_path, читающий пароль с /dev/tty; возвращает (путь, файл следа)."""
    script = tmp_path / 'fake-age'
    script.write_text(FAKE_AGE.format(python=sys.executable))
    script.chmod(0o755)
    store = tmp_path / 'store.json'
    monkeypatch.setenv('FAKE_STORE', str(store))
    return str(script), store


def ok_run(_args, **_kw):
    return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')


def bad_run(_args, **_kw):
    return SimpleNamespace(returncode=2, stdout=b'', stderr=b'tar: boom\nsecond\n')


def make_bundle(work, name='anysda-vpn2-2026-01-01T000000Z'):
    bundle = work / name
    (bundle / 'etc-anysda').mkdir(parents=True)
    (bundle / 'etc-anysda' / 'файл с пробелом.txt').write_text('данные')
    (bundle / 'db.sqlite').write_bytes(b'\0\1\2')
    (bundle / 'manifest.json').write_text('{}')
    return name


def test_workdir_mode_prefix_and_cleanup(tmp_path):
    shredded = []

    def run(args, **_kw):
        shredded.append(args)
        return SimpleNamespace(returncode=0)

    with workdir(tmp_path, '.backup-tmp.', run=run) as work:
        assert work.parent == tmp_path and work.name.startswith('.backup-tmp.')
        assert oct(work.stat().st_mode & 0o777) == '0o700'
        (work / 'sub').mkdir()
        (work / 'a.txt').write_text('x')
        (work / 'sub' / 'b b.txt').write_text('y')
        os.symlink(work / 'a.txt', work / 'link')
    assert not work.exists()
    assert sorted(a[-1] for a in shredded) == sorted([str(work / 'a.txt'), str(work / 'sub' / 'b b.txt')])
    assert all(a[:2] == ['shred', '-uz'] for a in shredded)


def test_workdir_removed_after_exception_and_shred_failures(tmp_path):
    def broken(_args, **_kw):
        raise FileNotFoundError('shred')

    with pytest.raises(RuntimeError):
        with workdir(tmp_path, '.restore-tmp.', run=broken) as work:
            (work / 'f').write_text('x')
            raise RuntimeError('обрыв')
    assert not work.exists() and list(tmp_path.iterdir()) == []
    with workdir(tmp_path, 'p.', run=bad_run) as again:
        (again / 'f').write_text('x')
    assert not again.exists()


@NEEDS_TAR
def test_workdir_real_shred(tmp_path):
    with workdir(tmp_path, 'p.') as work:
        (work / 'f').write_text('x')
    assert not work.exists()


def test_wipe_variants(tmp_path):
    calls = []
    for run in (ok_run, bad_run, lambda *_a, **_k: (_ for _ in ()).throw(OSError('нет shred'))):
        victim = tmp_path / 'v'
        victim.write_text('x')
        wipe(victim, run=lambda a, **k: (calls.append(a), run(a, **k))[1])
        assert victim.exists() == (run is ok_run)
    assert calls[0] == ['shred', '-uz', str(tmp_path / 'v')]
    wipe(tmp_path / 'нет такого', run=bad_run)


def test_pack_argv_and_errors(tmp_path):
    seen = []
    pack(tmp_path, 'bundle', tmp_path / 'out.tar.gz', run=lambda a, **k: (seen.append(a), ok_run(a, **k))[1])
    assert seen == [['tar', '-C', str(tmp_path), '-czf', str(tmp_path / 'out.tar.gz'), 'bundle']]
    with pytest.raises(ArchiveError) as info:
        pack(tmp_path, 'bundle', tmp_path / 'o', run=bad_run)
    assert info.value.code == 2
    with pytest.raises(ArchiveError) as info:
        pack(tmp_path, 'b', tmp_path / 'o', run=lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError(2, 'нет')))
    assert info.value.code == 127


@NEEDS_TAR
def test_pack_matches_script_tar(tmp_path):
    # удалить вместе с bash-телом
    line = next(x for x in (LIB / 'anysda-backup.sh').read_text().splitlines() if x.startswith('tar -C "$WORK" -czf'))
    assert line == 'tar -C "$WORK" -czf "$ARCHIVE_PLAIN" "$BUNDLE"'
    work = tmp_path / 'w'
    work.mkdir()
    name = make_bundle(work)
    old = work / 'old.tgz'
    subprocess.run(['bash', '-c', line], check=True, env={'PATH': os.environ['PATH'], 'WORK': str(work),
                   'ARCHIVE_PLAIN': str(old), 'BUNDLE': name})
    new = work / 'new.tgz'
    pack(work, name, new)
    lists = [subprocess.run(['tar', '-tzf', str(p)], capture_output=True, text=True, check=True).stdout
             for p in (old, new)]
    assert lists[0] == lists[1] and lists[0].startswith(name + '/')
    assert f'{name}/etc-anysda/файл с пробелом.txt' in lists[0].splitlines()


def test_encrypt_via_run_substitute(tmp_path):
    src, dst = tmp_path / 's', tmp_path / 'd.age'
    src.write_text('plain')
    seen = []

    def run(argv, passphrase, prompts, *, timeout):
        seen.append((argv, passphrase, prompts, timeout))
        dst.write_bytes(b'x')
        return 0

    encrypt(src, dst, 'пароль', run=run, timeout=7, age='/x/age')
    assert seen == [(['/x/age', '-p', '-o', str(dst), str(src)], 'пароль',
                     (b'Enter passphrase', b'Confirm passphrase'), 7)]
    assert not src.exists()


@pytest.mark.parametrize(('rc', 'make'), [(0, True), (1, True), (127, True), (0, False)])
def test_encrypt_failures_code_3(tmp_path, rc, make):
    src, dst = tmp_path / 's', tmp_path / 'd.age'
    src.write_text('plain')

    def run(_argv, _pw, _prompts, *, timeout):
        assert timeout == archive.TIMEOUT
        if not make:
            raise OSError('нет age')
        dst.write_bytes(b'')
        return rc

    with pytest.raises(ArchiveError) as info:
        encrypt(src, dst, 'секрет-пароль', run=run)
    assert info.value.code == 3 and str(dst) in info.value.message
    assert 'секрет-пароль' not in info.value.message and not src.exists()


def test_decrypt_via_run_substitute_and_failure(tmp_path):
    src, dst = tmp_path / 's.age', tmp_path / 'p'
    src.write_text('x')
    seen = []

    def good(argv, passphrase, prompts, *, timeout):
        seen.append((argv, passphrase, prompts, timeout))
        dst.write_bytes(b'data')
        return 0

    decrypt(src, dst, 'pw', run=good, age='/x/age')
    assert seen == [(['/x/age', '-d', '-o', str(dst), str(src)], 'pw', (b'Enter passphrase',), 60)]
    for rc in (1, 0):
        def bad(_a, _p, _pr, *, rc=rc, **_kw):
            dst.write_bytes(b'' if rc == 0 else b'junk')
            return rc
        with pytest.raises(ArchiveError) as info:
            decrypt(src, dst, 'pw', run=bad)
        assert info.value.code == 4 and f'age exit={rc}' in info.value.message and not dst.exists()


@pytest.mark.parametrize('password', PASSWORDS)
def test_pty_roundtrip_fake_age(tmp_path, fake, password):
    binary, store = fake
    plain, enc, back = tmp_path / 'p', tmp_path / 'e', tmp_path / 'b'
    plain.write_bytes(b'\0secret bytes\n')
    encrypt(plain, enc, password, age=binary)
    assert enc.read_bytes().startswith(b'ENC\n') and not plain.exists()
    decrypt(enc, back, password, age=binary)
    assert back.read_bytes() == b'\0secret bytes\n'
    seen = store.read_text()
    assert '"argv": ["-d", "-o"' in seen
    with pytest.raises(ArchiveError) as info:
        decrypt(enc, tmp_path / 'wrong', password + 'x', age=binary)
    assert info.value.code == 4 and not (tmp_path / 'wrong').exists()


def test_password_not_in_cmdline_environ_or_errors(tmp_path, fake, monkeypatch):
    binary, store = fake
    monkeypatch.setenv('FAKE_MODE', 'fail')
    secret = 'zq-unique-секрет'
    plain, enc = tmp_path / 'p', tmp_path / 'e'
    plain.write_text('x')
    with pytest.raises(ArchiveError) as info:
        encrypt(plain, enc, secret, age=binary)
    assert secret not in str(info.value) and secret not in repr(info.value)
    monkeypatch.delenv('FAKE_MODE')
    plain.write_text('x')
    encrypt(plain, enc, secret, age=binary)
    seen = store.read_text()
    assert secret not in seen and secret not in os.environ.get('ANYSDA_BACKUP_PASSPHRASE', '')


def test_pty_empty_result_and_missing_binary_and_timeout(tmp_path, fake, monkeypatch):
    binary, _store = fake
    plain, enc = tmp_path / 'p', tmp_path / 'e'
    plain.write_text('x')
    monkeypatch.setenv('FAKE_MODE', 'empty')
    with pytest.raises(ArchiveError) as info:
        encrypt(plain, enc, 'pw', age=binary)
    assert info.value.code == 3
    with pytest.raises(ArchiveError) as info:
        decrypt(enc, tmp_path / 'o', 'pw', age=str(tmp_path / 'нет-такого'))
    assert info.value.code == 4 and 'exit=127' in info.value.message
    monkeypatch.setenv('FAKE_MODE', 'hang')
    with pytest.raises(ArchiveError) as info:
        decrypt(enc, tmp_path / 'o', 'pw', age=binary, timeout=0.3)
    assert info.value.code == 4 and 'exit=-9' in info.value.message


def test_unpack_argv_error_text_and_roundtrip(tmp_path):
    seen = []
    unpack(tmp_path / 'a.tgz', tmp_path, run=lambda a, **k: (seen.append(a), ok_run(a, **k))[1])
    assert seen == [['tar', '-C', str(tmp_path), '-xzf', str(tmp_path / 'a.tgz')]]
    with pytest.raises(ArchiveError) as info:
        unpack(tmp_path / 'a.tgz', tmp_path, run=bad_run)
    assert info.value.code == 5
    assert info.value.message.splitlines() == [
        'anysda-restore: tar -xzf провалился — архив повреждён (после успешной дешифровки)',
        '  tar: boom', '  second']
    with pytest.raises(ArchiveError):
        unpack(tmp_path, tmp_path, run=lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError(2, 'нет')))


@NEEDS_TAR
def test_unpack_real_tar_and_corrupt(tmp_path):
    work = tmp_path / 'w'
    work.mkdir()
    name = make_bundle(work)
    tgz = tmp_path / 'a.tgz'
    pack(work, name, tgz)
    dest = tmp_path / 'dest'
    dest.mkdir()
    unpack(tgz, dest)
    assert (dest / name / 'etc-anysda' / 'файл с пробелом.txt').read_text() == 'данные'
    tgz.write_bytes(tgz.read_bytes()[:40])
    with pytest.raises(ArchiveError) as info:
        unpack(tgz, tmp_path / 'dest2')
    assert info.value.code == 5


def old_expect(script, marker, tmp_path, **names):
    """Тело expect-скрипта из bash-файла дословно, с подставленными путями."""
    text = (LIB / script).read_text()
    start = text.index(marker)
    body = text[text.index('\n', start) + 1:]
    body = body[:body.index('\nEXPECT\n')].replace('\\$', '$')
    for key, value in names.items():
        body = body.replace('"$' + key + '"', '"' + str(value) + '"')
    path = tmp_path / f'old-{script}.exp'
    path.write_text(body + '\n')
    return path


def run_old(exp, password):
    env = {**os.environ, 'ANYSDA_BACKUP_PASSPHRASE': password}
    return subprocess.run(['expect', '-f', str(exp)], env=env, capture_output=True, check=False).returncode


@NEEDS_AGE
@pytest.mark.parametrize('password', PASSWORDS)
def test_old_expect_and_new_python_interoperate(tmp_path, password):
    # удалить вместе с bash-телом
    plain, enc, back = tmp_path / 'p', tmp_path / 'e.age', tmp_path / 'b'
    plain.write_bytes(b'payload\n' * 100)
    old_enc = tmp_path / 'old.age'
    exp = old_expect('anysda-backup.sh', 'expect <<EXPECT', tmp_path, ARCHIVE_AGE=old_enc, ARCHIVE_PLAIN=plain)
    assert run_old(exp, password) == 0 and old_enc.stat().st_size > 0
    decrypt(old_enc, back, password)
    assert back.read_bytes() == b'payload\n' * 100
    keep = tmp_path / 'keep'
    keep.write_bytes(plain.read_bytes())
    encrypt(plain, enc, password)
    out = tmp_path / 'old-out'
    exp = old_expect('anysda-restore.sh', 'expect <<EXPECT', tmp_path, ARCHIVE_PLAIN=out, ARCHIVE=enc)
    assert run_old(exp, password) == 0 and out.read_bytes() == keep.read_bytes()


@NEEDS_AGE
def test_wrong_password_real_age_code_4_no_output(tmp_path):
    plain, enc, out = tmp_path / 'p', tmp_path / 'e.age', tmp_path / 'o'
    plain.write_text('data')
    encrypt(plain, enc, 'right')
    with pytest.raises(ArchiveError) as info:
        decrypt(enc, out, 'wrong')
    assert info.value.code == 4 and not out.exists()
    garbage = tmp_path / 'g.age'
    garbage.write_text('not age at all')
    with pytest.raises(ArchiveError):
        decrypt(garbage, out, 'right')
    assert not out.exists()
