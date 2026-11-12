"""wg0.conf outside apply: key and section names are read like wg reads them.

wg and wg-quick take `privatekey`, `[PEER]` and so on in any case, but only
ASCII letters: `PrivateKey` with the Kelvin sign is an unknown line for wg.
Each reader is cut out of its script and run on the same file in three forms.
"""
import base64
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import vpn_roles
from wg_root import as_root

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))

SERVER = base64.b64encode(bytes(range(32))).decode()
FRESH = base64.b64encode(bytes(range(32, 64))).decode()
PEER, OLD = (base64.b64encode(bytes([n] * 32)).decode() for n in (201, 202))
PSK_ONE, PSK_TWO = (base64.b64encode(bytes([n] * 32)).decode() for n in (211, 212))

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


# Stub of wg: pubkey of an empty key fails, genkey gives a key the host never had.
WG_STUB = ('wg() { if [[ $1 == genkey ]]; then echo ' + FRESH + '; return; fi\n'
           '  local key; key=$(cat); [[ -n $key ]] || return 1; echo ' + FRESH + '; }\n')


def pairs(text):
    """(name in lower case, value) for each ASCII `name = value` line."""
    return [(m[1].lower(), m[2]) for m in re.finditer(r'(?m)^\s*([A-Za-z]+)\s*=\s*(.*?)\s*$', text)]


def wireguard_rerun(style):
    """The wireguard role's wg-identity step on a host that has only wg0.conf."""
    with tempfile.TemporaryDirectory() as directory:
        etc = Path(directory) / 'etc-wireguard'
        (etc / 'keys').mkdir(parents=True)
        (etc / 'wg0.conf').write_text(conf(style))
        result = vpn_roles.wg_identity(etc, stub=WG_STUB)
        priv = etc / 'keys/server.priv'
        return result.returncode, priv.read_text().strip() if priv.exists() else None, result.stderr


@as_root
class WgConfKeyCaseTests(unittest.TestCase):
    """Edges 1-5: every reader takes the mixed-case file like wg does."""


    def test_4_wireguard_rerun_keeps_server_key(self):
        code, priv, stderr = wireguard_rerun('mixed')
        self.assertEqual((code, priv), (0, SERVER), stderr)

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
                done = subprocess.run(['env', f'PYTHONPATH={ROOT}/infra/vpn2/src', 'python3', '-B', '-m', 'vpn2.cli', 'backup', '--check-bundle', directory],
                                      env=dict(os.environ, ANYSDA_LIB_DIR=str(ROOT / 'infra/lib')),
                                      capture_output=True, text=True, timeout=60, check=False)
            code, lines, err = done.returncode, done.stdout.splitlines(), done.stderr
            self.assertEqual(code, 3, (lines, err))
            self.assertTrue([line for line in lines if 'PublicKey' in line], (lines, err))
            for value in (SERVER, PEER, OLD):
                self.assertNotIn(value, '\n'.join(lines) + err)
        with self.subTest('4 28-wireguard'):
            code, priv, stderr = wireguard_rerun(style)
            self.assertNotEqual(code, 0)
            self.assertIn('cannot replace an active WireGuard private key', stderr)
            self.assertNotEqual(priv, FRESH)

    def test_7_canonical_config_reads_as_before(self):
        style = 'canonical'
        with self.subTest('4 28-wireguard'):
            code, priv, stderr = wireguard_rerun(style)
            self.assertEqual((code, priv), (0, SERVER), stderr)


if __name__ == '__main__':
    unittest.main()
