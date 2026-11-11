import contextlib
import io
import json
import shutil
import sqlite3
import subprocess

import pytest
from vpn2 import wg
from vpn2.backup import check
from vpn2.cli import backup as cli_backup

# Ключи ненастоящие: собраны из частей.
K1, K2, K3 = ('A' * 42 + c + '=' for c in 'BCD')
PSK = 'P' * 42 + 'Q='
STAMP = '2026-01-02T030405Z'
NOW = 1767323045  # STAMP в секундах эпохи
SCHEMA = ('CREATE TABLE clients (id INTEGER, frozen_manual INTEGER, expires_at INTEGER);'
          'CREATE TABLE devices (id INTEGER, name TEXT, client_id INTEGER, deleting INTEGER, ovpn_cert TEXT,'
          ' wg_public_key TEXT, wg_preshared_key TEXT, wg_ip TEXT,'
          ' ikev2_username TEXT, ikev2_password TEXT, ikev2_ip TEXT);'
          'CREATE TABLE access_operations (client_id INTEGER, status TEXT, payload TEXT);')


def peer(key, psk=PSK, allowed=('10.0.0.2/32',)):
    return {'key': key, 'preshared_key': psk, 'allowed': list(allowed)}


def swan(*devs):
    """anysda-clients.conf: (номер, пользователь, пароль) - соединение и секрет EAP."""
    text = ''.join(f'  anysda-dev-{n}: connections.x\n' for n, _, _ in devs)
    return text + ''.join(f'eap-{n} {{ id = "{u}" secret = "{p}" }}\n' for n, u, p in devs)


def build(root, *, devices=(), clients=(), ops=(), peers=None, swanctl=None, db=True, manifest=True, crl=None):
    """Набор: БД из строк devices/clients, wg0.conf (peers - список или текст), swanctl, манифест."""
    root.mkdir(parents=True, exist_ok=True)
    if db:
        con = sqlite3.connect(root / 'db.sqlite')
        con.executescript(SCHEMA)
        con.executemany('INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?,?,?)', devices)
        con.executemany('INSERT INTO clients VALUES (?,?,?)', clients)
        con.executemany('INSERT INTO access_operations VALUES (?,?,?)', ops)
        con.commit()
        con.close()
    if peers is not None:
        (root / 'etc-wireguard').mkdir()
        (root / 'etc-wireguard/wg0.conf').write_text(peers if isinstance(peers, str) else json.dumps(peers))
    if swanctl is not None:
        (root / 'etc-swanctl/conf.d').mkdir(parents=True)
        (root / 'etc-swanctl/conf.d/anysda-clients.conf').write_text(swanctl)
    if crl is not None:
        (root / 'etc-openvpn/server/pki').mkdir(parents=True)
        (root / 'etc-openvpn/server/pki/crl.pem').write_text(crl)
    if manifest:
        (root / 'manifest.json').write_text(json.dumps({'timestamp': STAMP, 'components': {}}))
    return root


def dev(n, name='', client=1, deleting=0, cert=None, key=K1, psk=PSK, ip='10.0.0.2', user='u', pw='p'):
    return (n, name, client, deleting, cert, key, psk, ip, user, pw, '10.1.0.2')


CLIENT = (1, 0, None)


def run_py(root, monkeypatch=None, fake=None):
    if fake is not None:
        monkeypatch.setattr(wg, 'conf_peers', fake)
    out, err = io.StringIO(), io.StringIO()
    code = check.check_bundle(root, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def stub_conf_peers(data, **_):
    text = data.decode()
    if text.startswith('ERR:'):
        raise (wg.WgUnchecked if 'нечем' in text else wg.WgError)(text[4:])
    return json.loads(text)


# --- значения на каждый код ---
def test_code2_not_a_bundle(tmp_path):
    code, out, err = run_py(tmp_path)
    assert (code, out) == (2, '')
    assert err == (f"anysda-backup: --check-bundle: '{tmp_path}' — не распакованный бандл (нет manifest.json); "
                   'укажи каталог anysda-vpn2-* из архива\n')


@pytest.mark.parametrize('argv', [['--check-bundle'], ['--check-bundle', '']])
def test_command_without_path_is_code2(argv, capsys):
    """Скрипт на `--check-bundle` без каталога отвечал тем же отказом, а не разбором аргументов."""
    assert cli_backup.main(argv) == 2
    assert capsys.readouterr() == ('', "anysda-backup: --check-bundle: '' — не распакованный бандл "
                                       '(нет manifest.json); укажи каталог anysda-vpn2-* из архива\n')


def test_code0_without_db_and_good_bundle(tmp_path, monkeypatch):
    assert run_py(build(tmp_path / 'a', db=False)) == (0, '', '')
    good = build(tmp_path / 'b', devices=[dev(1), dev(2, key=K2, user='v', pw='w')], clients=[CLIENT],
                 peers=[peer(K1), peer(K2)], swanctl=swan((1, 'u', 'p'), (2, 'v', 'w')))
    assert run_py(good, monkeypatch, stub_conf_peers) == (0, '', '')


def test_code3_lines_in_order(tmp_path, monkeypatch):
    root = build(tmp_path / 'b', devices=[dev(1, ' Тел  efon\n'), dev(2, key=K2, psk='X' * 44), dev(3, key=K3, deleting=1)],
                 clients=[CLIENT], peers=[peer(K1), peer(K2), peer(K3), peer('Z' * 43 + '=')],
                 swanctl=swan((2, 'bad', 'p'), (3, 'u', 'p'), (9, 'u', 'p')))
    # K1 без учётки IKEv2; у 2 - не тот пароль и ключ; у 3 пир и учётка при deleting; пира Z нет в БД
    code, out, err = run_py(root, monkeypatch, stub_conf_peers)
    assert code == 3 and err == ''
    assert out.splitlines() == [
        'устройство 2: ключ пира WireGuard не совпадает с БД',
        'устройство 3: пир WireGuard у отключённого устройства',
        'пир WireGuard ' + 'Z' * 43 + '=: устройства нет в БД',
        'устройство 1 «Тел efon»: нет учётки IKEv2',
        'устройство 2: учётка IKEv2 не совпадает с БД',
        'устройство 3: учётка IKEv2 у отключённого устройства',
        'устройство 9: учётка IKEv2 есть, устройства нет в БД']


def test_code3_missing_peer(tmp_path, monkeypatch):
    root = build(tmp_path / 'b', devices=[dev(1)], clients=[CLIENT], peers=[], swanctl=swan((1, 'u', 'p')))
    assert run_py(root, monkeypatch, stub_conf_peers) == (3, 'устройство 1: нет пира WireGuard\n', '')


def test_code5_unchecked_after_bad(tmp_path, monkeypatch):
    plain = build(tmp_path / 'a', devices=[dev(1)], clients=[CLIENT], peers='ERR:wg0.conf: проверить нечем - нет wg')
    assert run_py(plain, monkeypatch, stub_conf_peers) == (5, 'wg0.conf: проверить нечем - нет wg\n', '')
    both = build(tmp_path / 'b', devices=[dev(1)], clients=[CLIENT], peers='ERR:wg0.conf: проверить нечем - нет wg',
                 swanctl='')
    code, out, _ = run_py(both, monkeypatch, stub_conf_peers)
    assert (code, out.splitlines()) == (3, ['устройство 1: нет учётки IKEv2', 'wg0.conf: проверить нечем - нет wg'])


def test_wg_rejects_file_is_complaint(tmp_path, monkeypatch):
    root = build(tmp_path / 'b', devices=[dev(1)], clients=[CLIENT], peers='ERR:wg0.conf: wg отвергает файл: x')
    assert run_py(root, monkeypatch, stub_conf_peers) == (3, 'wg0.conf: wg отвергает файл: x\n', '')


def test_external_program_failure_is_unchecked(tmp_path):
    root = build(tmp_path / 'b', devices=[dev(1)], clients=[CLIENT], peers='[]')

    def broken(argv, **_):
        return subprocess.CompletedProcess(argv, 120, b'', b'no netns\n')

    def missing(argv, **_):
        raise FileNotFoundError(argv[0])

    def slow(argv, timeout=30, **_):
        raise subprocess.TimeoutExpired(argv, timeout)

    for runner, text in ((broken, 'wg0.conf: проверить нечем - wg0 в netns: no netns\n'),
                         (missing, 'wg0.conf: проверить нечем - нет setpriv\n'),
                         (slow, 'wg0.conf: проверить нечем - wg не ответил за 30 с\n')):
        out, err = io.StringIO(), io.StringIO()
        assert check.check_bundle(root, run=runner, out=out, err=err) == 5
        assert (out.getvalue(), err.getvalue()) == (text, '')


def test_transitional_devices_are_not_checked(tmp_path, monkeypatch):
    now = check._timestamp(build(tmp_path / 't'))
    assert now == NOW
    clients = [(1, 0, None), (2, 1, None), (3, 0, now + 60), (4, 0, now - 10), (5, 0, now + 500), (6, 0, now - 500)]
    devices = [dev(n, client=n, key=f'{c}' * 43 + '=', ip=f'10.0.0.{n}') for n, c in
               zip(range(1, 8), 'ABCDEFG', strict=True)]
    ops = [(1, 'pending', json.dumps({'retired': [{'wgPublicKey': 'R', 'id': 7}, 5]})), (9, 'done', '')]
    root = build(tmp_path / 'b', devices=devices, clients=clients, ops=ops, peers=[], swanctl='')
    # 1 - pending, 3/4 - срок в пределах двух минут: молчим; 2 заморожен, 6 истёк, 7 без клиента: пира нет и не надо
    code, out, _ = run_py(root, monkeypatch, stub_conf_peers)
    assert (code, out.splitlines()) == (3, ['устройство 5: нет пира WireGuard', 'устройство 5: нет учётки IKEv2'])


def test_corrupt_database_exit1(tmp_path):
    root = build(tmp_path / 'b', db=False)
    (root / 'db.sqlite').write_bytes(b'not a database' * 100)
    code, out, err = run_py(root)
    assert (code, out, err) == (1, '', 'DatabaseError: file is not a database\n')


def test_check_leaves_no_open_connection(tmp_path, monkeypatch):
    """Срез в режиме WAL: после сверки следующий писатель (перенос отзывов) сворачивает журнал в файл БД."""
    root = build(tmp_path / 'b', devices=[dev(1)], clients=[CLIENT], peers=[peer(K1)], swanctl=swan((1, 'u', 'p')))
    database = root / 'db.sqlite'
    with contextlib.closing(sqlite3.connect(database)) as con:
        con.execute('PRAGMA journal_mode=wal')
    assert run_py(root, monkeypatch, stub_conf_peers) == (0, '', '')
    with contextlib.closing(sqlite3.connect(database)) as con, con:
        con.execute('UPDATE devices SET name = ? WHERE id = 1', ('после сверки',))
    assert not (root / 'db.sqlite-wal').exists()
    with contextlib.closing(sqlite3.connect(f'file:{database}?immutable=1', uri=True)) as con:
        assert con.execute('SELECT name FROM devices').fetchall() == [('после сверки',)]


def make_pki(base):
    """Свой CA через openssl: два сертификата, первый отозван. -> (pem первого, pem второго, crl.pem)."""
    base.mkdir()
    (base / 'index.txt').write_text('')
    (base / 'serial').write_text('0A\n')
    (base / 'crlnumber').write_text('01\n')
    (base / 'new').mkdir()
    (base / 'ca.cnf').write_text(f'[ca]\ndefault_ca = d\n[d]\ndatabase = {base}/index.txt\nnew_certs_dir = {base}/new\n'
                                 f'serial = {base}/serial\ncrlnumber = {base}/crlnumber\ndefault_md = sha256\n'
                                 'default_days = 30\ndefault_crl_days = 30\npolicy = p\n[p]\ncommonName = supplied\n'
                                 '[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = ca\n')

    def ssl(*args):
        subprocess.run(['openssl', *map(str, args)], check=True, capture_output=True)

    ca = ['-config', base / 'ca.cnf', '-cert', base / 'ca.pem', '-keyfile', base / 'ca.key']
    ssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', base / 'ca.key', '-out', base / 'ca.pem',
        '-config', base / 'ca.cnf', '-days', 30)
    for n in 'ab':
        ssl('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', base / f'{n}.key', '-out', base / f'{n}.csr',
            '-subj', f'/CN={n}')
        ssl('ca', '-batch', *ca, '-in', base / f'{n}.csr', '-out', base / f'{n}.pem', '-notext')
    ssl('ca', *ca, '-revoke', base / 'a.pem')
    ssl('ca', *ca, '-gencrl', '-out', base / 'crl.pem')
    return [(base / f).read_text() for f in ('a.pem', 'b.pem', 'crl.pem')]


@pytest.mark.skipif(shutil.which('openssl') is None, reason='нужен openssl')
def test_revoked_certificate_is_reported(tmp_path):
    revoked, live, crl = make_pki(tmp_path / 'pki')
    root = build(tmp_path / 'b', devices=[dev(1, 'Отозван', cert=revoked), dev(2, cert=live), dev(3, deleting=1, cert=revoked)],
                 clients=[CLIENT], crl=crl)
    code, out, err = run_py(root)
    assert (code, out, err) == (3, 'устройство 1 «Отозван»: сертификат A отозван в CRL\n', '')
