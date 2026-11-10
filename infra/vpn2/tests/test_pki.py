from pathlib import Path

import pytest

from vpn2 import pki
from vpn2.pki import PkiError, Row

FIXTURES = Path(__file__).parent / 'fixtures' / 'pki'
KEY_LINES = ['0123456789abcdef0123456789ABCDEF'] * 16
GOOD_KEY = '\n'.join(['#', '# 2048 bit OpenVPN static key', '#', pki.KEY_BEGIN, *KEY_LINES, pki.KEY_END]) + '\n'
CERT = '/CN=dev_7'
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
    assert [row.reason for row in rows] == ['', 'certificateHold', '']
    assert rows[1].revoked == '260101000000Z,certificateHold'


def test_by_cn_matches_whole_name_only():
    rows = pki.index(INDEX)
    assert [row.serial for row in pki.by_cn(rows, 'dev_7')] == ['0A', '0C']
    assert [row.serial for row in pki.by_cn(rows, 'dev_70')] == ['0B']
    assert pki.by_cn(rows, 'dev_') == []
    assert pki.by_cn([], 'dev_7') == []


def test_by_cn_separators_slash_and_comma():
    text = ''.join(f'V\t3\t\t{n:02X}\tunknown\t{subject}\n' for n, subject in enumerate([
        '/O=a,b/CN=dev_7', '/CN=dev_7/O=x', 'CN=dev_7,O=x', 'O=x,CN=dev_7', '/CN=dev_7,x', '/XCN=dev_7',
    ]))
    rows = pki.index(text)
    assert [row.serial for row in pki.by_cn(rows, 'dev_7')] == ['00', '01']
    assert [row.serial for row in pki.by_cn(rows, 'dev_7', comma=True)] == ['00', '01', '02', '03', '04']


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
