import ctypes
import errno
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import vpn2
from vpn2 import pki
from vpn2.pki import PkiError, PkiIdentity

SRC = str(Path(vpn2.__file__).resolve().parent.parent)


def tree(root):
    """Каталог как {путь: байты}, подкаталоги - None."""
    return {str(p.relative_to(root)): (None if p.is_dir() else p.read_bytes())
            for p in sorted(Path(root).rglob('*'))}


def kit(tmp_path, name='pki'):
    target = tmp_path / name
    (target / 'sub').mkdir(parents=True)
    (target / 'ca.crt').write_text('OLD CA\n')
    (target / 'sub' / 'ключ.key').write_text('старый\n')
    return target


def test_exception_keeps_target_byte_for_byte_and_cleans_up(tmp_path):
    target = kit(tmp_path)
    before, siblings = tree(target), sorted(os.listdir(tmp_path))
    with pytest.raises(RuntimeError, match='builder'):
        with pki.transaction(target) as work:
            (work / 'ca.crt').write_text('NEW\n')
            (work / 'sub' / 'ключ.key').unlink()
            raise RuntimeError('builder')
    assert tree(target) == before
    assert sorted(os.listdir(tmp_path)) == ['.pki.lock', *siblings]  # файл замка остаётся, как в старом коде
    with pki.transaction(target, timeout=0):  # замок отпущен
        pass


def test_success_swaps_whole_directory(tmp_path):
    target = kit(tmp_path)
    inode = target.stat().st_ino
    with pki.transaction(target) as work:
        assert tree(work) == tree(target) and work.parent == tmp_path
        assert work.name.startswith('pki.tmp.')
        (work / 'ca.crt').write_text('NEW CA\n')
        (work / 'sub' / 'ключ.key').unlink()
        (work / 'server.key').write_text('S\n')
        assert (target / 'ca.crt').read_text() == 'OLD CA\n'
    assert tree(target) == {'ca.crt': b'NEW CA\n', 'server.key': b'S\n', 'sub': None}
    assert target.stat().st_ino != inode
    assert sorted(os.listdir(tmp_path)) == ['.pki.lock', 'pki']


def test_first_install_renames_work_into_place(tmp_path):
    target = tmp_path / 'new' / 'пки'  # родителя тоже нет, имя не-ASCII
    with pki.transaction(target) as work:
        assert tree(work) == {}
        (work / 'ca.crt').write_text('CA\n')
    assert tree(target) == {'ca.crt': b'CA\n'}
    assert target.stat().st_mode & 0o777 == 0o700  # mkdtemp под umask 077
    assert sorted(os.listdir(target.parent)) == ['.пки.lock', 'пки']


def test_modes_umask_and_repeat(tmp_path):
    target = kit(tmp_path)
    target.chmod(0o755)
    (target / 'ca.crt').chmod(0o644)
    umask = os.umask(0o022)
    try:
        with pki.transaction(target) as work:
            assert (work / 'ca.crt').stat().st_mode & 0o777 == 0o600  # --no-preserve=mode
            (work / 'made').write_text('x')
            assert (work / 'made').stat().st_mode & 0o777 == 0o600
        assert os.umask(0o022) == 0o022  # umask вернулся
    finally:
        os.umask(umask)
    assert target.stat().st_mode & 0o777 == 0o755
    first = tree(target)
    with pki.transaction(target):
        pass
    assert tree(target) == first


def test_empty_target_stays_empty(tmp_path):
    target = tmp_path / 'pki'
    target.mkdir()
    with pki.transaction(target) as work:
        assert tree(work) == {}
    assert tree(target) == {}


class NoRenameat2:
    def __init__(self, code):
        self.code = code

    def renameat2(self, *_args):
        ctypes.set_errno(self.code)
        return -1


@pytest.mark.parametrize('code', [errno.ENOSYS, errno.EINVAL])
def test_without_renameat2_existing_target_fails_untouched(tmp_path, monkeypatch, code):
    monkeypatch.setattr(pki, '_libc', lambda: NoRenameat2(code))
    target = kit(tmp_path)
    before = tree(target)
    with pytest.raises(OSError) as caught:
        with pki.transaction(target) as work:
            (work / 'ca.crt').write_text('NEW\n')
    assert caught.value.errno == code and caught.value.filename == str(target)
    assert tree(target) == before
    assert sorted(os.listdir(tmp_path)) == ['.pki.lock', 'pki']


def test_without_renameat2_first_install_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(pki, '_libc', lambda: NoRenameat2(errno.ENOSYS))
    with pki.transaction(tmp_path / 'pki') as work:
        (work / 'a').write_text('a')
    assert tree(tmp_path / 'pki') == {'a': b'a'}


CHILD = '''
import sys
from pathlib import Path
from vpn2 import pki
from vpn2.fsutil import LockBusy
target, name, timeout = sys.argv[1], sys.argv[2], sys.argv[3]
print('start', flush=True)
try:
    with pki.transaction(target, timeout=None if timeout == '-' else float(timeout)) as work:
        print('in', (work / 'v').read_text(), work, flush=True)
        sys.stdin.readline()
        (work / 'v').write_text(name)
except LockBusy:
    print('busy', flush=True)
'''


def child(target, name, timeout='-'):
    env = {**os.environ, 'PYTHONPATH': SRC}
    return subprocess.Popen([sys.executable, '-c', CHILD, str(target), name, timeout], env=env, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)


def finish(proc, line=''):
    out, _ = proc.communicate(line, timeout=30)
    assert proc.returncode == 0
    return out


def test_second_process_times_out_or_waits_and_sees_first_result(tmp_path):
    target = tmp_path / 'pki'
    target.mkdir()
    (target / 'v').write_text('old')
    first = child(target, 'A')
    assert first.stdout.readline() == 'start\n'
    assert first.stdout.readline().startswith('in old ')
    assert finish(child(target, 'X', '0.2')) == 'start\nbusy\n'  # первый держит замок
    second = child(target, 'B')
    assert second.stdout.readline() == 'start\n'
    finish(first, '\n')
    assert finish(second, '\n').startswith('in A ')  # копия снята после подмены первым
    assert (target / 'v').read_text() == 'B'
    assert sorted(os.listdir(tmp_path)) == ['.pki.lock', 'pki']


def test_leftover_of_killed_run_is_bypassed(tmp_path):
    target = tmp_path / 'pki'
    target.mkdir()
    (target / 'v').write_text('old')
    killed = child(target, 'K')
    killed.stdout.readline()
    leftover = Path(killed.stdout.readline().split()[2])
    killed.send_signal(signal.SIGKILL)
    killed.communicate(timeout=30)
    assert leftover.parent == tmp_path and (leftover / 'v').read_text() == 'old'
    with pki.transaction(target, timeout=0) as work:  # замок умер вместе с процессом
        assert work != leftover
        (work / 'v').write_text('new')
    assert (target / 'v').read_text() == 'new'
    assert sorted(os.listdir(tmp_path)) == ['.pki.lock', 'pki', leftover.name]  # как старый код: не трогает


TLS = '\n'.join([pki.KEY_BEGIN, *['0123456789abcdef' * 2] * 16, pki.KEY_END]) + '\n'


def done(code=0, out='', err=''):
    return subprocess.CompletedProcess([], code, out, err)


class Fake:
    """Подмена run: нет файла после -in/-key/-CAfile - код 1; -out и --genkey создают файл."""

    def __init__(self, fail=None, tls=TLS, pub=lambda _argv: 'PUB\n'):
        self.calls, self.fail, self.tls, self.pub = [], fail, tls, pub

    def __call__(self, argv, data=None):
        assert data is None
        self.calls.append(argv)
        if self.fail and self.fail(argv):
            return done(1, err='warning\nboom\n')
        needed = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg in ('-in', '-key', '-CAfile')]
        if any(not Path(p).is_file() for p in needed + argv[-1:] * (argv[1] == 'verify')):
            return done(1, err='No such file')
        if argv[0] == 'openvpn':
            Path(argv[-1]).write_text(self.tls)
        elif '-out' in argv:
            Path(argv[argv.index('-out') + 1]).write_text(argv[1] + '\n')
        return done(out=self.pub(argv) if {'-pubout', '-pubkey'} & set(argv) else '')


def stage_argv(d):
    """argv стадии 29 на пустом каталоге, по порядку."""
    o, ca, key, crt = 'openssl', f'{d}/ca.crt', f'{d}/server.key', f'{d}/server.crt'
    ec = ['-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:prime256v1']
    pair = lambda k, c: [[o, 'pkey', '-in', k, '-check', '-noout'], [o, 'x509', '-in', c, '-noout', '-checkend', '0'],
                         [o, 'pkey', '-in', k, '-pubout'], [o, 'x509', '-in', c, '-pubkey', '-noout']]
    crl = [o, 'crl', '-in', f'{d}/crl.pem', '-noout', '-verify', '-CAfile', ca]
    return [
        [o, 'pkey', '-in', f'{d}/ca.key', '-check', '-noout'], [o, 'x509', '-in', ca, '-noout', '-checkend', '0'],
        [o, 'genpkey', *ec, '-out', f'{d}/ca.key'], [o, 'x509', '-in', ca, '-noout', '-checkend', '0'],
        [o, 'req', '-x509', '-new', '-key', f'{d}/ca.key', '-days', '3650', '-sha256', '-out', ca,
         '-subj', '/CN=anysda-vpn2 CA', '-config', f'{d}/openssl.cnf', '-extensions', 'v3_ca'],
        *pair(f'{d}/ca.key', ca), [o, 'verify', '-CAfile', ca, ca],
        [o, 'pkey', '-in', key, '-check', '-noout'], [o, 'x509', '-in', crt, '-noout', '-checkend', '0'],
        [o, 'genpkey', *ec, '-out', key],
        [o, 'pkey', '-in', key, '-check', '-noout'], [o, 'x509', '-in', crt, '-noout', '-checkend', '0'],
        [o, 'req', '-new', '-key', key, '-out', f'{d}/server.csr', '-subj', '/CN=anysda-vpn2-server',
         '-config', f'{d}/openssl.cnf'],
        [o, 'ca', '-batch', '-notext', '-config', f'{d}/build.cnf', '-extensions', 'server_ext', '-days', '3650',
         '-in', f'{d}/server.csr', '-out', crt],
        ['openvpn', '--genkey', 'secret', f'{d}/tls-crypt.key'],
        crl, [o, 'ca', '-config', f'{d}/build.cnf', '-gencrl', '-out', f'{d}/crl.pem'],
        *pair(key, crt), [o, 'verify', '-CAfile', ca, '-purpose', 'sslserver', crt], crl,
    ]


def test_build_openvpn_argv_order_like_stage_29(tmp_path):
    fake = Fake()
    pki.build_openvpn(tmp_path, run=fake)
    assert fake.calls == stage_argv(tmp_path)
    assert (tmp_path / 'openssl.cnf').read_text() == pki.openssl_cnf()  # каталог прода, не рабочий
    assert (tmp_path / 'serial').read_text() == (tmp_path / 'crlnumber').read_text() == '01\n'
    assert (tmp_path / 'index.txt').read_text() == ''
    names = {p.name: p.stat().st_mode & 0o777 for p in tmp_path.iterdir()}
    assert 'build.cnf' not in names and 'server.csr' not in names and (tmp_path / 'newcerts').is_dir()
    assert [names[n] for n in ('ca.key', 'server.key', 'tls-crypt.key')] == [0o600] * 3
    assert [names[n] for n in ('ca.crt', 'server.crt', 'crl.pem')] == [0o644] * 3


def test_build_openvpn_repeat_reissues_nothing(tmp_path):
    pki.build_openvpn(tmp_path, run=Fake())
    (tmp_path / 'index.txt').write_text('V\t300101000000Z\t\t0A\tunknown\t/CN=dev_7\n')
    before = tree(tmp_path)
    fake = Fake()
    pki.build_openvpn(tmp_path, run=fake)
    assert tree(tmp_path) == before
    assert not [a for a in fake.calls if a[1] in ('genpkey', 'req', 'ca', '--genkey')]


@pytest.mark.parametrize('step', ['req', 'ca', 'genpkey', '--genkey'])
def test_build_openvpn_failure_in_transaction_keeps_kit(tmp_path, step):
    target = tmp_path / 'pki'
    with pki.transaction(target) as work:
        (work / 'old').write_text('прежний\n')
    before = tree(target)
    with pytest.raises(PkiError) as caught:
        with pki.transaction(target) as work:
            pki.build_openvpn(work, run=Fake(fail=lambda argv: argv[1] == step))
    assert str(caught.value).endswith(': boom') and caught.value.code == 1
    assert tree(target) == before
    assert sorted(os.listdir(tmp_path)) == ['.pki.lock', 'pki']


def test_build_openvpn_missing_program_or_timeout(tmp_path, monkeypatch):
    def timeout(argv, **_kwargs):
        raise PkiError(f'{argv[0]}: timeout')
    with pytest.raises(PkiError, match='^openssl: timeout$'):
        pki.build_openvpn(tmp_path, run=timeout)
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    with pytest.raises(PkiError, match='^openssl: No such file or directory$'):
        pki.build_openvpn(tmp_path, run=pki.run)


def test_build_openvpn_live_ca_with_broken_key_is_identity(tmp_path):
    (tmp_path / 'ca.crt').write_text('CA\n')
    with pytest.raises(PkiIdentity, match='valid certificate but missing/invalid private key'):
        pki.build_openvpn(tmp_path, run=Fake())
    assert (tmp_path / 'ca.crt').read_text() == 'CA\n' and not (tmp_path / 'ca.key').exists()


def test_build_openvpn_mismatched_pair_and_bad_tls_key(tmp_path):
    (tmp_path / 'a').mkdir()
    with pytest.raises(PkiError, match=r'mismatched key/certificate: .*/ca\.crt$'):
        pki.build_openvpn(tmp_path / 'a', run=Fake(pub=lambda argv: argv[1]))
    (tmp_path / 'b').mkdir()
    with pytest.raises(PkiError, match=r'^PKI: invalid tls-crypt key: .*/tls-crypt\.key$'):
        pki.build_openvpn(tmp_path / 'b', run=Fake(tls=TLS.replace('0', 'g')))


def ca_identity(directory):
    pem = (directory / 'ca.crt').read_text()
    finger = pki.run(['openssl', 'x509', '-noout', '-fingerprint', '-sha256'], data=pem).stdout
    return pki.serial(pem), finger


@pytest.mark.skipif(shutil.which('openssl') is None or shutil.which('openvpn') is None,
                    reason='нужны openssl и openvpn')
def test_build_openvpn_real_kit_verifies_and_repeat_keeps_ca(tmp_path):
    target = tmp_path / 'pki'
    with pki.transaction(target) as work:
        pki.build_openvpn(work)
    ca, crt, crl = target / 'ca.crt', target / 'server.crt', target / 'crl.pem'
    assert pki.verify(ca, crt, purpose='sslserver') and pki.verify(ca, crt, crl=crl)
    assert pki.pair_valid(target / 'server.key', crt) and pki.tls_crypt_valid((target / 'tls-crypt.key').read_text())
    assert 'CN=anysda-vpn2-server' in subprocess.run(['openssl', 'x509', '-noout', '-subject', '-nameopt', 'RFC2253', '-in', str(crt)], capture_output=True, text=True, check=True).stdout
    assert pki.index((target / 'index.txt').read_text())[0].subject == '/CN=anysda-vpn2-server'
    first, before = ca_identity(target), tree(target)
    assert first[0] is not None and 'Fingerprint=' in first[1]
    with pki.transaction(target) as work:
        pki.build_openvpn(work)
    assert ca_identity(target) == first and tree(target) == before
