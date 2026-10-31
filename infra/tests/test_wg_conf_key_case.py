"""wg0.conf outside apply: key and section names are read like wg reads them.

wg and wg-quick take `privatekey`, `[PEER]` and so on in any case, but only
ASCII letters: `PrivateKey` with the Kelvin sign is an unknown line for wg.
Each reader is cut out of its script and run on the same file in three forms.
"""
import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from wg_root import as_root

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))

SERVER = base64.b64encode(bytes(range(32))).decode()
FRESH = base64.b64encode(bytes(range(32, 64))).decode()
PEER, OLD, NEW = (base64.b64encode(bytes([n] * 32)).decode() for n in (201, 202, 203))
PSK_ONE, PSK_TWO, PSK_NEW = (base64.b64encode(bytes([n] * 32)).decode() for n in (211, 212, 213))

NAMES = ('Interface', 'Address', 'ListenPort', 'FwMark', 'PrivateKey', 'PublicKey', 'PresharedKey', 'AllowedIPs')
STYLES = {
    'canonical': (dict(zip(NAMES, NAMES)), ('Peer', 'Peer')),
    'mixed': ({'Interface': 'interface', 'Address': 'address', 'ListenPort': 'listenport', 'FwMark': 'FWMARK',
               'PrivateKey': 'privatekey', 'PublicKey': 'publickey', 'PresharedKey': 'presharedkey',
               'AllowedIPs': 'allowedips'}, ('PEER', 'peer')),
    # One letter wg does not take for the ASCII one: U+0130, U+017F, U+212A.
    'lookalike': ({'Interface': '\u0130nterface', 'Address': 'Addre\u017fs', 'ListenPort': 'Li\u017ftenPort',
                   'FwMark': 'FwMar\u212a', 'PrivateKey': 'Private\u212aey', 'PublicKey': 'Public\u212aey',
                   'PresharedKey': 'Pre\u017fharedKey', 'AllowedIPs': 'AllowedIP\u017f'}, ('Peer', 'Peer')),
}


def conf(style):
    n, peers = STYLES[style]
    return (f'[{n["Interface"]}]\n{n["Address"]} = 10.66.66.1/24\n{n["ListenPort"]} = 51820\n'
            f'{n["FwMark"]} = 0x66\n{n["PrivateKey"]} = {SERVER}\n\n'
            f'[{peers[0]}]\n{n["PublicKey"]} = {PEER}\n{n["PresharedKey"]} = {PSK_ONE}\n'
            f'{n["AllowedIPs"]} = 10.66.66.2/32\n\n'
            f'[{peers[1]}]\n{n["PublicKey"]} = {OLD}\n{n["PresharedKey"]} = {PSK_TWO}\n'
            f'{n["AllowedIPs"]} = 10.66.66.3/32\n')


def source(path):
    return (ROOT / path).read_text()


def cut(text, start, end):
    assert start in text, start
    body = text.split(start, 1)[1]
    assert end in body, end
    return start + body.split(end, 1)[0]


def bash(script, directory):
    env = dict(os.environ, LC_ALL='C.UTF-8')
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=directory, env=env,
                          text=True, capture_output=True, timeout=15)


def python(block, **names):
    scope = {'re': re, 'os': os, 'hashlib': hashlib, **names}
    exec(block, scope)
    return scope


# Stub of wg: pubkey of an empty key fails, genkey gives a key the host never had.
WG_STUB = ('wg() { if [[ $1 == genkey ]]; then echo ' + FRESH + '; return; fi\n'
           '  local key; key=$(cat); [[ -n $key ]] || return 1; echo "pub-$key"; }\n')


def pairs(text):
    """(name in lower case, value) for each ASCII `name = value` line."""
    return [(m[1].lower(), m[2]) for m in re.finditer(r'(?m)^\s*([A-Za-z]+)\s*=\s*(.*?)\s*$', text)]


def backup_check(style):
    """anysda-backup.sh --check-bundle: complaints about WireGuard peers."""
    block = cut(source('infra/lib/anysda-backup.sh'), "wg = root / 'etc-wireguard/wg0.conf'", '\nswan = root')
    rows = [(1, True, (PEER, PSK_ONE, '10.66.66.2')),
            (2, None, (NEW, PSK_NEW, '10.66.66.3'))]  # reissued: old key still on its address
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'etc-wireguard').mkdir()
        (root / 'etc-wireguard/wg0.conf').write_text(conf(style))
        scope = python(block, root=root, bad=[], retired_keys=set(), label=lambda d: f'устройство {d}',
                       device_columns={'wg_public_key', 'wg_preshared_key', 'wg_ip'},
                       devices=lambda *_fields: iter(rows))
    return scope['bad']


def restore_server_priv(style):
    """anysda-restore.sh: server.priv taken from [Interface] of the archived wg0.conf."""
    block = cut(source('infra/lib/anysda-restore.sh'), 'WG_BUNDLE="$BUNDLE_DIR/etc-wireguard"',
                '# Стадия 30 ждёт')
    with tempfile.TemporaryDirectory() as directory:
        bundle = Path(directory) / 'bundle/etc-wireguard'
        bundle.mkdir(parents=True)
        (bundle / 'wg0.conf').write_text(conf(style))
        result = bash('BUNDLE_DIR="$PWD/bundle"\n' + WG_STUB + block, directory)
        return result.returncode, (bundle / 'server.priv').read_text().strip()


def restore_wg_key(style):
    """anysda-restore.sh wg_key(): fingerprint of the server key in a wg0.conf directory."""
    text = source('infra/lib/anysda-restore.sh')
    block = cut(text, 'def digest(', '\n\n\ndef index_rows')
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / 'wg0.conf').write_text(conf(style))
        return python(block)['wg_key'](directory)


def restore_live_peers(style):
    """anysda-restore.sh: peer keys of the live /etc/wireguard/wg0.conf."""
    block = cut(source('infra/lib/anysda-restore.sh'), 'if live_peers is not None:\n',
                '\nif live_ike is not None:')
    return python(block, live_peers=conf(style))['live_peers']


def restore_drop_peers(style):
    """anysda-restore.sh: wg0.conf of the archive without peers of revoked devices."""
    text = source('infra/lib/anysda-restore.sh')
    block = cut(text, 'def rewrite(', '\n\nif (ike_ids')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'wg0.conf'
        path.write_text(conf(style))
        python(block, wg_keys={PEER}, wg_path=str(path))
        return path.read_text()


def load_verify():
    spec = importlib.util.spec_from_file_location('verify_traffic_case', ROOT / 'infra/lib/verify-traffic.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VT = load_verify()


def verify_conf_peers(style):
    return [(p['key'], p['preshared_key'], p['allowed']) for p in VT.conf_peers(conf(style))]


def verify_syncconf_body(style):
    """verify-traffic.py restore_wg_apply(): body of `wg syncconf wg0 /dev/stdin`."""
    sent = []
    saved = VT.WORK, VT.run, VT.restore_file
    with tempfile.TemporaryDirectory() as directory:
        VT.WORK = Path(directory)
        (VT.WORK / 'wg0.conf').write_text(conf(style))
        VT.run = lambda *args, data=None, **_kw: sent.append((args, data))
        VT.restore_file = lambda *_args: None
        try:
            VT.restore_wg_apply()
        finally:
            VT.WORK, VT.run, VT.restore_file = saved
    [(args, data)] = sent
    assert args[:3] == ('wg', 'syncconf', 'wg0'), args
    return data


def wireguard_rerun(style):
    """28-wireguard.sh wireguard_keys_build on a host that has only wg0.conf."""
    script = source('infra/scripts/28-wireguard.sh')
    block = cut(script, 'wireguard_keys_build() {', '\n}\n') + '\n}\n'
    with tempfile.TemporaryDirectory() as directory:
        etc = Path(directory) / 'etc-wireguard'
        (etc / 'keys').mkdir(parents=True)
        (etc / 'wg0.conf').write_text(conf(style))
        result = bash(WG_STUB + block.replace('/etc/wireguard', str(etc)) +
                      f'wireguard_keys_build {etc}/keys\n', directory)
        priv = etc / 'keys/server.priv'
        return result.returncode, priv.read_text().strip() if priv.exists() else None, result.stderr


def svc_conf_iface(style):
    """anysda-svc.sh wg0_conf_iface: addresses of [Interface] in wg0.conf."""
    block = cut(source('infra/lib/anysda-svc.sh'), 'wg0_conf_iface() {', '\n}\n') + '\n}\n'
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'wg0.conf'
        path.write_text(conf(style))
        result = bash(block.replace('/etc/wireguard/wg0.conf', str(path)) + 'wg0_conf_iface\n', directory)
        return result.returncode, result.stdout.split('\n')[:-1]


PEERS = [(PEER, PSK_ONE, ['10.66.66.2/32']), (OLD, PSK_TWO, ['10.66.66.3/32'])]


@as_root
class WgConfKeyCaseTests(unittest.TestCase):
    """Edges 1-5: every reader takes the mixed-case file like wg does."""

    def test_1_backup_check_knows_every_peer(self):
        self.assertEqual(backup_check('mixed'), [])

    def test_2a_restore_takes_server_priv_from_interface(self):
        self.assertEqual(restore_server_priv('mixed'), (0, SERVER))

    def test_2b_restore_wg_key_finds_server_key(self):
        self.assertEqual(restore_wg_key('mixed'), hashlib.sha256(SERVER.encode()).hexdigest())

    def test_2c_restore_finds_live_peers(self):
        self.assertEqual(restore_live_peers('mixed'), {PEER, OLD})

    def test_2d_restore_drops_peer_of_revoked_device(self):
        text = restore_drop_peers('mixed')
        self.assertNotIn(PEER, text, 'peer of the revoked device is left in wg0.conf')
        self.assertIn(OLD, text)
        self.assertIn(SERVER, text)

    def test_3a_verify_conf_peers_finds_peers(self):
        self.assertEqual(verify_conf_peers('mixed'), PEERS)

    def test_3b_verify_syncconf_body_keeps_interface(self):
        body = pairs(verify_syncconf_body('mixed'))
        for line in (('privatekey', SERVER), ('listenport', '51820'), ('fwmark', '0x66'),
                     ('publickey', PEER), ('presharedkey', PSK_ONE), ('publickey', OLD)):
            self.assertIn(line, body)

    def test_4_wireguard_rerun_keeps_server_key(self):
        code, priv, stderr = wireguard_rerun('mixed')
        self.assertEqual((code, priv), (0, SERVER), stderr)

    def test_5_svc_reads_interface_section(self):
        self.assertEqual(svc_conf_iface('mixed'), (0, ['addr 10.66.66.1/24']))

    def test_6_non_ascii_name_is_not_a_key_anywhere(self):
        style = 'lookalike'
        with self.subTest('1 backup'):
            # Both [Peer] lack PublicKey for wg: the whole check-bundle fails like wg rejects the file.
            import json
            import sqlite3
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'manifest.json').write_text(json.dumps({'timestamp': '2026-10-30T120000Z'}))
                con = sqlite3.connect(root / 'db.sqlite')
                con.execute('CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, wg_public_key TEXT, '
                            'wg_preshared_key TEXT, wg_ip TEXT)')
                con.execute('INSERT INTO devices VALUES (1, ?, ?, ?, ?)', ('one', PEER, PSK_ONE, '10.66.66.2'))
                con.commit()
                con.close()
                (root / 'etc-wireguard').mkdir()
                (root / 'etc-wireguard/wg0.conf').write_text(conf(style))
                done = subprocess.run(['bash', str(ROOT / 'infra/lib/anysda-backup.sh'), '--check-bundle', directory],
                                      env=dict(os.environ, ANYSDA_LIB_DIR=str(ROOT / 'infra/lib')),
                                      capture_output=True, text=True, timeout=60, check=False)
            code, lines, err = done.returncode, done.stdout.splitlines(), done.stderr
            self.assertEqual(code, 3, (lines, err))
            self.assertTrue([line for line in lines if 'PublicKey' in line], (lines, err))
            for value in (SERVER, PEER, OLD):
                self.assertNotIn(value, '\n'.join(lines) + err)
        with self.subTest('2a restore server.priv'):
            self.assertEqual(restore_server_priv(style), (5, ''))
        with self.subTest('2b restore wg_key'):
            self.assertIsNone(restore_wg_key(style))
        with self.subTest('2c restore live peers'):
            self.assertEqual(restore_live_peers(style), set())
        with self.subTest('2d restore drop peers'):
            self.assertEqual(restore_drop_peers(style), conf(style))
        with self.subTest('3a verify conf_peers'):
            with self.assertRaises(ValueError) as caught:
                verify_conf_peers(style)
            self.assertIn('PublicKey', str(caught.exception))
        with self.subTest('3b verify syncconf'):
            # No body with the keys is built, nothing goes to `wg syncconf`.
            with self.assertRaises(ValueError) as caught:
                verify_syncconf_body(style)
            self.assertIn('PublicKey', str(caught.exception))
            for value in (SERVER, '51820', '0x66', PEER, OLD):
                self.assertNotIn(value, str(caught.exception))
        with self.subTest('4 28-wireguard'):
            code, priv, stderr = wireguard_rerun(style)
            self.assertNotEqual(code, 0)
            self.assertIn('cannot replace an active WireGuard private key', stderr)
            self.assertNotEqual(priv, FRESH)
        with self.subTest('5 svc'):
            self.assertEqual(svc_conf_iface(style), (0, []))

    def test_7_canonical_config_reads_as_before(self):
        style = 'canonical'
        with self.subTest('1 backup'):
            self.assertEqual(backup_check(style), [])
        with self.subTest('2a restore server.priv'):
            self.assertEqual(restore_server_priv(style), (0, SERVER))
        with self.subTest('2b restore wg_key'):
            self.assertEqual(restore_wg_key(style), hashlib.sha256(SERVER.encode()).hexdigest())
        with self.subTest('2c restore live peers'):
            self.assertEqual(restore_live_peers(style), {PEER, OLD})
        with self.subTest('2d restore drop peers'):
            text = restore_drop_peers(style)
            self.assertNotIn(PEER, text)
            self.assertIn(OLD, text)
        with self.subTest('3a verify conf_peers'):
            self.assertEqual(verify_conf_peers(style), PEERS)
        with self.subTest('3b verify syncconf'):
            body = pairs(verify_syncconf_body(style))
            for line in (('privatekey', SERVER), ('listenport', '51820'), ('fwmark', '0x66'), ('publickey', PEER)):
                self.assertIn(line, body)
        with self.subTest('4 28-wireguard'):
            code, priv, stderr = wireguard_rerun(style)
            self.assertEqual((code, priv), (0, SERVER), stderr)
        with self.subTest('5 svc'):
            self.assertEqual(svc_conf_iface(style), (0, ['addr 10.66.66.1/24']))


if __name__ == '__main__':
    unittest.main()
