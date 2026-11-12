import shutil
import subprocess
from pathlib import Path

import pytest

from vpn2 import pki
from vpn2.pki import PkiError, Row

FIXTURES = Path(__file__).parent / 'fixtures' / 'pki'
KEY_LINES = ['0123456789abcdef0123456789ABCDEF'] * 16
GOOD_KEY = '\n'.join(['#', '# 2048 bit OpenVPN static key', '#', pki.KEY_BEGIN, *KEY_LINES, pki.KEY_END]) + '\n'
CERT = '/CN=dev_7'
PEM_BEGIN = '-----BEGIN ' + 'PRIVATE KEY' + '-----'
PEM_END = '-----END ' + 'PRIVATE KEY' + '-----'
INDEX = (f'V\t300101000000Z\t\t0A\tunknown\t{CERT}\n'
         'R\t300101000000Z\t260101000000Z,certificateHold\t0B\tunknown\t/CN=dev_70\n'
         'R\t300101000000Z\t260101000000Z\t0C\tunknown\t/CN=dev_7\n')


def test_tls_crypt_valid_accepts_genkey_output():
    assert pki.tls_crypt_valid(GOOD_KEY)
    assert pki.tls_crypt_valid(GOOD_KEY.rstrip('\n'))
    assert pki.tls_crypt_valid(GOOD_KEY.replace('#\n', '\n  \t\n#x\n'))


@pytest.mark.parametrize('lines', [
    KEY_LINES[:-1] + ['0' * 31],  # 511 hex
    KEY_LINES + ['0'],  # 513 hex
    KEY_LINES[:-1] + ['0' * 31 + 'g'],  # не hex
    KEY_LINES[:-1],  # 480 hex
])
def test_tls_crypt_valid_rejects_wrong_size_or_chars(lines):
    assert not pki.tls_crypt_valid('\n'.join([pki.KEY_BEGIN, *lines, pki.KEY_END]) + '\n')


@pytest.mark.parametrize('text', [
    '', '\n', '# only comment\n',
    '\n'.join([pki.KEY_BEGIN, *KEY_LINES]) + '\n',  # нет END
    '\n'.join([*KEY_LINES, pki.KEY_END]) + '\n',  # нет BEGIN
    GOOD_KEY + 'trailing\n',
    GOOD_KEY + GOOD_KEY,
    GOOD_KEY.replace('\n', '\r\n'),
    GOOD_KEY.replace(KEY_LINES[0], ' ' + KEY_LINES[0], 1),
    GOOD_KEY.replace(pki.KEY_BEGIN, pki.KEY_BEGIN + ' '),
    '\n'.join([pki.KEY_BEGIN, pki.KEY_BEGIN, *KEY_LINES, pki.KEY_END]),
    'ключ\n',
])
def test_tls_crypt_valid_rejects_broken_text(text):
    assert not pki.tls_crypt_valid(text)


def test_index_parses_six_columns_and_skips_the_rest():
    rows = pki.index(INDEX)
    assert rows[0] == Row('V', '300101000000Z', '', '0A', 'unknown', CERT)
    assert [row.serial for row in rows] == ['0A', '0B', '0C']
    assert pki.index(INDEX) == rows
    assert pki.index('') == []
    assert pki.index('\n\n') == []
    assert pki.index('V\tx\t\t0A\tunknown\n') == []  # 5 колонок
    assert pki.index('a\tb\tc\td\te\tf\tg\n') == []  # 7 колонок
    assert len(pki.index('\n' + INDEX + '\nshort\n')) == 3


def test_index_row_reason_is_after_comma():
    rows = pki.index(INDEX)
    assert [row.revoked.partition(',')[2] for row in rows] == ['', 'certificateHold', '']
    assert rows[1].revoked == '260101000000Z,certificateHold'


def test_by_cn_matches_whole_name_only():
    rows = pki.index(INDEX)
    assert [row.serial for row in pki.by_cn(rows, 'dev_7')] == ['0A', '0C']
    assert [row.serial for row in pki.by_cn(rows, 'dev_70')] == ['0B']
    assert pki.by_cn(rows, 'dev_') == []
    assert pki.by_cn([], 'dev_7') == []


def test_by_cn_separator_is_slash():
    text = ''.join(f'V\t3\t\t{n:02X}\tunknown\t{subject}\n' for n, subject in enumerate([
        '/O=a,b/CN=dev_7', '/CN=dev_7/O=x', 'CN=dev_7,O=x', 'O=x,CN=dev_7', '/CN=dev_7,x', '/XCN=dev_7',
    ]))
    rows = pki.index(text)
    assert [row.serial for row in pki.by_cn(rows, 'dev_7')] == ['00', '01']


def test_by_cn_cn_is_not_a_regex():
    rows = pki.index('V\t3\t\t0A\tunknown\t/CN=dev.7\nV\t3\t\t0B\tunknown\t/CN=devX7\n')
    assert [row.serial for row in pki.by_cn(rows, 'dev.7')] == ['0A']


def test_by_cn_bad_serial_skipped_or_strict_error():
    rows = pki.index('V\t3\t\tZZ\tunknown\t/CN=dev_7\nV\t3\t\t\tunknown\t/CN=dev_7\nV\t3\t\t0A\tunknown\t/CN=dev_7\n'
                     f'V\t3\t\t{"A" * 65}\tunknown\t/CN=dev_7\nV\t3\t\tZZ\tunknown\t/CN=dev_8\n')
    assert [row.serial for row in pki.by_cn(rows, 'dev_7')] == ['0A']
    with pytest.raises(PkiError, match='bad serial'):
        pki.by_cn(rows, 'dev_7', strict=True)
    assert pki.by_cn(rows[-1:], 'dev_9', strict=True) == []


def test_openssl_cnf_default_is_the_stage_heredoc_byte_for_byte():
    expected = (FIXTURES / 'openssl.cnf').read_bytes()
    assert pki.openssl_cnf().encode() == expected
    assert pki.openssl_cnf('/etc/openvpn/server/pki').encode() == expected


def test_openssl_cnf_changes_only_the_dir_line():
    default, work = pki.openssl_cnf().splitlines(), pki.openssl_cnf('/tmp/w x/pki').splitlines()
    assert len(default) == len(work)
    assert [(a, b) for a, b in zip(default, work) if a != b] == [
        ('dir              = /etc/openvpn/server/pki', 'dir              = /tmp/w x/pki')]
    assert pki.openssl_cnf(Path('/tmp/w')) == pki.openssl_cnf('/tmp/w')
    assert pki.openssl_cnf('/a') == pki.openssl_cnf('/a')


def test_openssl_cnf_sections_and_extensions():
    text = pki.openssl_cnf()
    server, client = text.split('[server_ext]')[1].split('[client_ext]')
    assert 'keyEncipherment' in server and 'keyEncipherment' not in client
    for line in ('default_md       = sha256', 'default_days     = 3650', 'unique_subject   = no',
                 'policy           = policy_anything', '[v3_ca]'):
        assert line in text
    assert text.endswith('subjectKeyIdentifier   = hash\nauthorityKeyIdentifier = keyid,issuer\n')


class Fake:
    """Подмена run: запоминает argv и data, отвечает по очереди или по первому совпадению."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, argv, *, data=None, **_):
        self.calls.append((list(argv), data))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def done(code=0, out='', err=''):
    return subprocess.CompletedProcess([], code, out, err)


def argvs(fake):
    return [argv for argv, _ in fake.calls]


def test_run_wraps_subprocess_text_mode():
    result = pki.run(['cat'], data='привет')
    assert (result.returncode, result.stdout) == (0, 'привет')
    assert pki.run(['sh', '-c', 'echo oops >&2; exit 3']).returncode == 3


def test_run_missing_program_and_timeout_are_pki_errors():
    with pytest.raises(PkiError, match='no-such-program-pki'):
        pki.run(['no-such-program-pki', '--secret-arg'])
    with pytest.raises(PkiError, match='timeout') as caught:
        pki.run(['sleep', '5'], timeout=0.05)
    assert caught.value.__cause__ is None


def test_checks_use_exact_argv_and_map_return_code():
    fake = Fake(done(0))
    assert pki.key_valid('/p/k.pem', run=fake) and pki.cert_valid(Path('/p/c.pem'), run=fake)
    assert pki.cert_valid('/p/c.pem', checkend=3600, run=fake)
    assert argvs(fake) == [['openssl', 'pkey', '-in', '/p/k.pem', '-check', '-noout'],
                           ['openssl', 'x509', '-in', '/p/c.pem', '-noout', '-checkend', '0'],
                           ['openssl', 'x509', '-in', '/p/c.pem', '-noout', '-checkend', '3600']]
    bad = Fake(done(1, err='boom'))
    assert not pki.key_valid('k', run=bad) and not pki.cert_valid('c', run=bad)
    gone = Fake(PkiError('openssl: not found'))
    assert not pki.key_valid('k', run=gone) and not pki.cert_valid('c', run=gone)
    assert not pki.verify('ca', 'c', run=gone) and not pki.pair_valid('k', 'c', run=gone)


def test_pair_valid_compares_public_keys():
    pub = '-----BEGIN PUBLIC KEY-----\nAAA\n-----END PUBLIC KEY-----\n'
    fake = Fake(done(0), done(0), done(0, pub), done(0, pub))
    assert pki.pair_valid('k', 'c', run=fake)
    assert argvs(fake)[2:] == [['openssl', 'pkey', '-in', 'k', '-pubout'],
                               ['openssl', 'x509', '-in', 'c', '-pubkey', '-noout']]
    other = Fake(done(0), done(0), done(0, pub), done(0, pub.replace('AAA', 'BBB')))
    assert not pki.pair_valid('k', 'c', run=other)
    empty = Fake(done(0), done(0), done(1), done(1))
    assert not pki.pair_valid('k', 'c', run=empty)
    assert not pki.pair_valid('k', 'c', run=Fake(done(1)))


def test_require_key_valid_key_does_nothing():
    fake = Fake(done(0))
    pki.require_key('k', 'c', run=fake)
    assert len(fake.calls) == 1


def test_require_key_broken_key_with_live_cert_is_identity_error():
    fake = Fake(done(1), done(0))
    with pytest.raises(pki.PkiIdentity, match='PKI: valid certificate but missing/invalid private key: /p/k'):
        pki.require_key('/p/k', '/p/c', run=fake)
    assert issubclass(pki.PkiIdentity, PkiError)
    assert all(argv[1] != 'genpkey' for argv in argvs(fake))


def test_require_key_generates_when_cert_is_dead():
    fake = Fake(done(1), done(1), done(0))
    pki.require_key('/p/k', '/p/c', run=fake)
    assert argvs(fake)[-1] == ['openssl', 'genpkey', '-algorithm', 'EC', '-pkeyopt',
                               'ec_paramgen_curve:prime256v1', '-out', '/p/k']


def test_verify_argv_plain_and_crl():
    fake = Fake(done(0))
    assert pki.verify('/p/ca.crt', '/p/c.pem', run=fake)
    assert pki.verify('/p/ca.crt', '/p/c.pem', crl='/p/crl.pem', run=fake)
    assert argvs(fake) == [
        ['openssl', 'verify', '-CAfile', '/p/ca.crt', '/p/c.pem'],
        ['openssl', 'verify', '-CAfile', '/p/ca.crt', '-CRLfile', '/p/crl.pem', '-crl_check', '/p/c.pem']]
    assert not pki.verify('ca', 'c', crl='crl', run=Fake(done(2, err='certificate revoked')))


def test_issue_argv_and_serial():
    fake = Fake(done(0), done(0), done(0, 'serial=0A\n'))
    assert pki.issue('/p/pki', 'dev_7', '/w/key.pem', '/w/csr.pem', '/w/cert.tmp', run=fake) == '0A'
    assert argvs(fake) == [
        ['openssl', 'req', '-new', '-key', '/w/key.pem', '-out', '/w/csr.pem', '-subj', '/CN=dev_7'],
        ['openssl', 'ca', '-batch', '-notext', '-config', '/p/pki/openssl.cnf', '-extensions', 'client_ext',
         '-days', '3650', '-in', '/w/csr.pem', '-out', '/w/cert.tmp'],
        ['openssl', 'x509', '-in', '/w/cert.tmp', '-noout', '-serial']]


def test_issue_failures_stop_at_the_failing_step():
    fake = Fake(done(0), done(1, err='x\nTXT_DB error number 2'))
    with pytest.raises(PkiError, match='openssl ca: TXT_DB error number 2') as caught:
        pki.issue('/p', 'dev_7', 'k', 'r', 'c', run=fake)
    assert caught.value.code == 1 and len(fake.calls) == 2
    with pytest.raises(PkiError, match='no serial'):
        pki.issue('/p', 'dev_7', 'k', 'r', 'c', run=Fake(done(0), done(0), done(0, 'junk\n')))
    with pytest.raises(PkiError, match='openssl req: bad'):
        pki.issue('/p', 'dev_7', 'k', 'r', 'c', run=Fake(done(4, err='bad')))


def test_revoke_argv_and_error_text():
    fake = Fake(done(0))
    pki.revoke('/p/pki', '/p/n/0A.pem', run=fake)
    assert argvs(fake) == [['openssl', 'ca', '-batch', '-config', '/p/pki/openssl.cnf', '-revoke', '/p/n/0A.pem']]
    with pytest.raises(PkiError) as caught:
        pki.revoke('/p', 'c', run=Fake(done(1, err='first\nAlready revoked\n')))
    assert str(caught.value) == 'openssl ca -revoke: Already revoked' and caught.value.code == 1
    with pytest.raises(PkiError, match=r'openssl ca -revoke: \?$'):
        pki.revoke('/p', 'c', run=Fake(done(1)))


def crl_maker(body='NEW CRL\n'):
    """run, который кладёт файл после `-out` у gencrl, как настоящий openssl."""
    calls = []

    def fake(argv, **_):
        calls.append(list(argv))
        if '-gencrl' in argv:
            Path(argv[argv.index('-out') + 1]).write_text(body)
        return done(0)
    fake.calls = calls
    return fake


def test_gencrl_verified_writes_0644_after_the_check(tmp_path):
    out = tmp_path / 'crl.pem'
    out.write_text('OLD')
    fake = crl_maker()
    pki.gencrl(tmp_path / 'pki', out, run=fake)
    tmp_crl = fake.calls[0][-1]
    assert fake.calls == [
        ['openssl', 'ca', '-batch', '-config', f'{tmp_path}/pki/openssl.cnf', '-gencrl', '-out', tmp_crl],
        ['openssl', 'crl', '-in', tmp_crl, '-noout', '-verify', '-CAfile', f'{tmp_path}/pki/ca.crt']]
    assert out.read_text() == 'NEW CRL\n' and out.stat().st_mode & 0o777 == 0o644
    assert sorted(p.name for p in tmp_path.iterdir()) == ['crl.pem']


def test_gencrl_unverifiable_crl_keeps_the_old_file(tmp_path):
    out = tmp_path / 'crl.pem'
    out.write_text('OLD')

    def fake(argv, **_):
        if '-gencrl' in argv:
            Path(argv[-1]).write_text('FOREIGN')
            return done(0)
        return done(2, err='verification failure')
    with pytest.raises(PkiError, match='openssl crl: verification failure'):
        pki.gencrl(tmp_path, out, run=fake)
    assert out.read_text() == 'OLD'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['crl.pem']


def test_gencrl_failure_of_openssl_ca(tmp_path):
    out = tmp_path / 'crl.pem'
    with pytest.raises(PkiError, match='gencrl: no index'):
        pki.gencrl(tmp_path, out, run=Fake(done(1, err='no index')))
    assert not out.exists()


def test_error_text_has_no_input_data_or_argv():
    secret = PEM_BEGIN + '\nTOPSECRETKEYBYTES\n' + PEM_END + '\n'

    def fake(*_, **__):
        return done(1, err='unable to load\n')
    with pytest.raises(PkiError) as caught:
        pki._checked(fake, ['openssl', 'pkey', '-in', '/x/TOPSECRETPATH'], 'openssl pkey', data=secret)
    assert 'TOPSECRET' not in str(caught.value) + repr(caught.value.args)
    assert str(caught.value) == 'openssl pkey: unable to load'
    with pytest.raises(PkiError) as timed:
        pki.run(['sleep', '5'], data=secret, timeout=0.05)
    assert 'TOPSECRET' not in str(timed.value)


real = pytest.mark.skipif(shutil.which('openssl') is None, reason='нужен openssl')


def make_ca(base):
    """Пустая база CA и самоподписанный корень, как стадия 29."""
    base.mkdir()
    (base / 'openssl.cnf').write_text(pki.openssl_cnf(base))
    (base / 'index.txt').write_text('')
    (base / 'serial').write_text('0A\n')
    (base / 'crlnumber').write_text('01\n')
    (base / 'newcerts').mkdir()
    pki.genkey(base / 'ca.key')
    pki._checked(pki.run, ['openssl', 'req', '-x509', '-new', '-key', str(base / 'ca.key'), '-days', '3650',
                           '-sha256', '-out', str(base / 'ca.crt'), '-subj', '/CN=anysda-vpn2 CA',
                           '-config', str(base / 'openssl.cnf'), '-extensions', 'v3_ca'], 'req')
    return base


@pytest.fixture
def ca(tmp_path):
    return make_ca(tmp_path / 'pki')


@real
def test_real_genkey_checks_and_require_key(tmp_path):
    key, cert = tmp_path / 'k.pem', tmp_path / 'c.pem'
    pki.require_key(key, cert)
    assert pki.key_valid(key) and key.read_text().startswith(PEM_BEGIN)
    before = key.read_bytes()
    pki.require_key(key, cert)
    assert key.read_bytes() == before
    assert not pki.key_valid(tmp_path / 'missing.pem')
    key.write_text('')
    assert not pki.key_valid(key) and not pki.cert_valid(key)
    key.write_text(PEM_BEGIN + '\nAAAA\n' + PEM_END + '\n')
    assert not pki.key_valid(key)


@real
def test_real_pair_valid_and_broken_key_with_live_cert(ca, tmp_path):
    assert pki.cert_valid(ca / 'ca.crt') and pki.cert_valid(ca / 'ca.crt', checkend=3600)
    assert not pki.cert_valid(ca / 'ca.crt', checkend=3650 * 86400 + 86400)
    assert pki.pair_valid(ca / 'ca.key', ca / 'ca.crt')
    other = tmp_path / 'other.pem'
    pki.genkey(other)
    assert not pki.pair_valid(other, ca / 'ca.crt')
    before = (ca / 'ca.key').read_bytes()
    (ca / 'ca.key').write_text('broken')
    with pytest.raises(pki.PkiIdentity):
        pki.require_key(ca / 'ca.key', ca / 'ca.crt')
    assert (ca / 'ca.key').read_text() == 'broken' and before != b'broken'


@real
def test_real_issue_revoke_gencrl_verify(ca, tmp_path):
    key, csr, cert = tmp_path / 'key.pem', tmp_path / 'csr.pem', tmp_path / 'cert.pem'
    pki.genkey(key)
    value = pki.issue(ca, 'dev_7', key, csr, cert)
    assert value == '0A' and pki.pair_valid(key, cert)
    rows = pki.by_cn(pki.index((ca / 'index.txt').read_text()), 'dev_7', strict=True)
    assert [(row.status, row.serial) for row in rows] == [('V', '0A')]
    assert (ca / 'newcerts' / '0A.pem').read_text() == cert.read_text()
    crl = ca / 'crl.pem'
    pki.gencrl(ca, crl)
    assert crl.stat().st_mode & 0o777 == 0o644
    assert pki.verify(ca / 'ca.crt', cert) and pki.verify(ca / 'ca.crt', cert, crl=crl)
    pki.revoke(ca, cert)
    pki.gencrl(ca, crl)
    rows = pki.by_cn(pki.index((ca / 'index.txt').read_text()), 'dev_7')
    assert [(row.status, row.revoked.partition(',')[2]) for row in rows] == [('R', '')]
    assert pki.verify(ca / 'ca.crt', cert)
    assert not pki.verify(ca / 'ca.crt', cert, crl=crl)
    again = pki.run(['openssl', 'verify', '-CAfile', str(ca / 'ca.crt'), '-CRLfile', str(crl), '-crl_check',
                     str(cert)])
    assert 'certificate revoked' in again.stdout + again.stderr
    assert pki.gencrl(ca, crl) is None and crl.read_text().startswith('-----BEGIN X509 CRL-----')


@real
def test_real_revoke_twice_and_failures(ca, tmp_path):
    key, cert = tmp_path / 'key.pem', tmp_path / 'cert.pem'
    pki.genkey(key)
    pki.issue(ca, 'dev_8', key, tmp_path / 'csr.pem', cert)
    pki.revoke(ca, cert)
    row, = pki.by_cn(pki.index((ca / 'index.txt').read_text()), 'dev_8')
    assert row.status == 'R'
    with pytest.raises(PkiError, match='openssl ca -revoke: ') as caught:
        pki.revoke(ca, cert)
    assert caught.value.code not in (None, 0)
    with pytest.raises(PkiError, match='openssl ca -revoke: '):
        pki.revoke(ca, tmp_path / 'no-such.pem')
    with pytest.raises(PkiError):
        pki.revoke(tmp_path / 'no-ca-dir', cert)


@real
def test_real_gencrl_with_mismatched_ca_fails_and_old_kept(ca, tmp_path):
    other = make_ca(tmp_path / 'other')
    crl = ca / 'crl.pem'
    pki.gencrl(ca, crl)
    old = crl.read_bytes()
    (ca / 'ca.crt').write_bytes((other / 'ca.crt').read_bytes())  # CA подменён: ключ не к сертификату
    with pytest.raises(PkiError, match='openssl ca -gencrl'):
        pki.gencrl(ca, crl)
    assert crl.read_bytes() == old
    assert sorted(p.name for p in ca.iterdir() if p.name.startswith('tmp')) == []

