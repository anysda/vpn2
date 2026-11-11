"""Отзывы после архива (vpn2.backup.revoke) на дереве архива и узла в tmp_path.

Внешний openssl подменён скриптом-заглушкой в PATH, пишущей argv в журнал.
"""
import os
import sqlite3
import stat
import sys

import pytest

from vpn2.backup.revoke import RevokeError, run


STUB = """#!%s
import os, re, sys
args = sys.argv[1:]
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(' '.join(args) + '\\n')
if args[:1] == ['x509']:
    text = sys.stdin.read()
    if '-serial' in args:
        print('serial=' + re.search(r'S=([0-9A-F]+)', text)[1])
    else:
        print('subject=CN=dev_' + re.search(r'D=(\\d+)', text)[1])
elif args[:2] == ['ca', '-batch'] and '-revoke' in args and os.environ.get('STUB_FAIL'):
    sys.stderr.write('first line\\nunable to revoke\\n')
    sys.exit(1)
elif '-gencrl' in args:
    open(args[args.index('-out') + 1], 'w').write('CRL\\n')
"""

CASES = {
    'nothing': dict(archive=[1, 2, 3], live=[1, 2, 3]),
    'wg': dict(archive=[1, 2, 3], live=[1, 3], cert=[], ike=[]),
    'cert': dict(archive=[1, 2, 3], live=[1, 3], wg=[], ike=[]),
    'cert_index': dict(archive=[1, 2, 3], live=[1, 3], live_revoked=[2], wg=[], ike=[]),
    'issued': dict(archive=[1, 2], live=[1, 2, 3], extra=[3]),
    'ikev2': dict(archive=[1, 2, 3], live=[1, 3], wg=[], cert=[]),
    'fail': dict(archive=[1, 2, 3], live=[1, 3], wg=[], ike=[], fail=True),
}


def cert(n):
    return f'-----BEGIN CERTIFICATE-----\nS={n:02X};D={n}\n-----END CERTIFICATE-----\n'


def build(root, case):
    """Дерево входных данных: root/bundle (архив) и root/live (узел)."""
    spec = {'wg': [1, 2, 3], 'cert': [1, 2, 3], 'ike': [1, 2, 3], 'live_revoked': [], 'extra': [], **CASES[case]}
    live, bundle = root / 'live', root / 'bundle'
    pki, apki = live / 'pki', bundle / 'etc-openvpn/server/pki'
    for d in (pki / 'newcerts', apki / 'newcerts', live / 'wg', live / 'ccd',
              bundle / 'etc-wireguard', bundle / 'etc-swanctl/conf.d'):
        d.mkdir(parents=True)
    for p in (pki, apki):
        (p / 'ca.crt').write_text('CA\n')
        (p / 'openssl.cnf').write_text('[ca]\ndir = /x\n')
        (p / 'serial').write_text('05\n')
        (p / 'crlnumber').write_text('03\n')
    (live / 'wg/wg0.conf').write_text('[Interface]\nPrivateKey = K\n')
    (bundle / 'etc-wireguard/wg0.conf').write_text('[Interface]\nPrivateKey = K\n' + ''.join(
        f'\n# peer {n}\n[Peer]\nPublicKey = PUB{n}=\nAllowedIPs = 10.0.0.{n}/32\n' for n in spec['archive']))
    for n in spec['archive'] + spec['extra']:
        row = f"{'R' if n in spec['live_revoked'] else 'V'}\t301231000000Z\t{'260101000000Z' if n in spec['live_revoked'] else ''}\t{n:02X}\tunknown\t/CN=dev_{n}\n"
        (pki / 'index.txt').open('a').write(row)
        if n in spec['archive']:
            (apki / 'index.txt').open('a').write(row.replace('R\t', 'V\t', 1).replace('260101000000Z', ''))
        (pki / 'newcerts' / f'{n:02X}.pem').write_text(cert(n))
    ike = ''.join(f'    eap-{n} {{\n        id = "u{n}"\n        secret = "pw{n}"\n    }}\n' for n in spec['ike'])
    conf = 'connections {\n    c {\n        remote_addrs = %any\n    }\n}\nsecrets {\n' + ike + '}\n'
    (bundle / 'etc-swanctl/conf.d/anysda-clients.conf').write_text(conf)
    (live / 'ike.conf').write_text(conf)
    columns = 'id INTEGER PRIMARY KEY AUTOINCREMENT, created_at, wg_private_key, wg_public_key, wg_preshared_key, ' \
              'ovpn_cert, ovpn_key, ikev2_username, ikev2_password'
    for path, ids in ((bundle / 'db.sqlite', spec['archive']), (live / 'db.sqlite', spec['archive'] + spec['extra'])):
        con = sqlite3.connect(path)
        con.execute(f'CREATE TABLE devices ({columns})')
        for n in ids:
            has = lambda kind: n in spec[kind]  # noqa: E731
            con.execute('INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?)', (
                n, f't{n}', f'priv{n}' if has('wg') else None, f'PUB{n}=' if has('wg') else None, None,
                cert(n) if has('cert') else None, f'key{n}' if has('cert') else None,
                f'u{n}' if has('ike') else None, f'pw{n}' if has('ike') else None))
        if path.parent == live:
            con.execute('DELETE FROM devices WHERE id NOT IN (%s)' % ','.join(map(str, spec['live'] + spec['extra'])))
        con.commit()
        con.close()
    return bundle, live


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


@pytest.fixture
def stub(tmp_path, monkeypatch):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    path = bindir / 'openssl'
    path.write_text(STUB % sys.executable)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv('PATH', f'{bindir}:{os.environ["PATH"]}')
    return tmp_path


def revoked(stub, case, monkeypatch, capsys):
    """Прогон run на дереве случая; возвращает ((код, stdout, ошибка), дерево, журнал openssl)."""
    if CASES[case].get('fail'):
        monkeypatch.setenv('STUB_FAIL', '1')
    root = stub / 'new'
    root.mkdir()
    bundle, live = build(root, case)
    log = stub / 'new.log'
    monkeypatch.setenv('STUB_LOG', str(log))
    capsys.readouterr()
    try:
        code, err = run(str(bundle), str(root / 'issued'), live_db=f'{live}/db.sqlite', live_pki=f'{live}/pki',
                        live_wg_dir=f'{live}/wg', live_ike_conf=f'{live}/ike.conf', live_ccd=f'{live}/ccd'), ''
    except RevokeError as error:
        code, err = error.code, error.message + '\n'
    return (code, capsys.readouterr().out, err), snapshot(root), log.read_text() if log.exists() else ''


def test_nothing_changed_makes_no_revoke_calls(stub, monkeypatch, capsys):
    (code, out, _), tree, log = revoked(stub, 'nothing', monkeypatch, capsys)
    assert code == 0
    assert 'применено поверх архива: 0' in out
    assert ' -revoke ' not in log and '-gencrl' not in log


def test_wg_peer_removed_from_bundle(stub, monkeypatch, capsys):
    _, tree, _ = revoked(stub, 'wg', monkeypatch, capsys)
    conf = tree['bundle/etc-wireguard/wg0.conf'].decode()
    assert 'PUB2=' not in conf and 'PUB1=' in conf and 'PUB3=' in conf


def test_cert_revoke_and_gencrl_argv(stub, monkeypatch, capsys):
    _, tree, log = revoked(stub, 'cert', monkeypatch, capsys)
    assert ' -revoke ' in log and ' -gencrl -out ' in log
    assert tree['bundle/etc-openvpn/server/pki/crl.pem'] == b'CRL\n'


def test_issued_after_archive_listed(stub, monkeypatch, capsys):
    _, tree, _ = revoked(stub, 'issued', monkeypatch, capsys)
    assert tree['issued'] == b'3\n'


def test_ikev2_account_removed(stub, monkeypatch, capsys):
    _, tree, _ = revoked(stub, 'ikev2', monkeypatch, capsys)
    conf = tree['bundle/etc-swanctl/conf.d/anysda-clients.conf'].decode()
    assert 'eap-2' not in conf and 'eap-1' in conf


def test_openssl_failure_gives_code_1_and_message(stub, monkeypatch, capsys):
    (code, _, err), _, _ = revoked(stub, 'fail', monkeypatch, capsys)
    assert code == 1
    assert err == 'openssl ca -revoke: unable to revoke\n'
