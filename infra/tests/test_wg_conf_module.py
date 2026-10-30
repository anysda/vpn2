"""wg0.conf outside apply: a [Peer] section without a public key, the wg_conf.py module on the node.

wg rejects the whole file when a [Peer] section has `PublicKey =` with no
value or no PublicKey at all. The backup check (anysda-backup.sh
--check-bundle) and verify-traffic.py must fail like wg does, not drop the
section with its addresses. Stage 26 installs wg_conf.py where the node keeps
its libraries, /usr/local/lib/anysda/, before the backup script that imports
it, and restore runs the check with the module from there wherever restore
itself lies.
"""
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import tempfile
import unittest

from test_wg_conf_allowedips_net import G4, G6, apply_gate
from test_wg_conf_allowedips_prefix import (
    A4, A6, B4, GONE, OLD, PEER, PSK_GONE, PSK_ONE, PSK_TWO, ROOT, SERVER, VT, cut, source)

LIB = ROOT / 'infra/lib'
NODE_LIB = '/usr/local/lib/anysda'
# Sections after the header: (PublicKey value or None for no line, PresharedKey or None, AllowedIPs line).
BASE = [(PEER, PSK_ONE, '10.66.66.2/32, fd66:66::2/128'), (GONE, PSK_GONE, f'{G4}, {G6}'),
        (OLD, PSK_TWO, f'{A4}, {A6}')]
FORMS = {
    'pubkey-blank': BASE[:2] + [('', PSK_TWO, f'{A4}, {A6}')],
    'pubkey-missing': BASE[:2] + [(None, PSK_TWO, f'{A4}, {A6}')],
    'pubkey-blank-after': BASE + [('', None, B4)],
    'pubkey-missing-after': BASE + [(None, None, B4)],
}
CANONICAL = BASE
# `wg setconf wg0 /dev/stdin` of each form (no Address line) on a fresh wg0 in a netns
# (Linux 7.0, wireguard-tools 1.0.20250521): the whole file rejected with this error.
WG_ORACLE = {
    'pubkey-blank': 'Line unrecognized: `PublicKey=\'',
    'pubkey-missing': 'A peer is missing a public key',
    'pubkey-blank-after': 'Line unrecognized: `PublicKey=\'',
    'pubkey-missing-after': 'A peer is missing a public key',
}
# Devices of the bundle's db.sqlite: (id, name, key, psk, address) - every peer of BASE is an active device.
DEVICES = [(1, 'one', PEER, PSK_ONE, '10.66.66.2'), (2, 'old', OLD, PSK_TWO, '10.66.66.3'),
           (3, 'gone', GONE, PSK_GONE, '10.66.66.4')]


def conf(sections, address=True):
    """As 28-wireguard writes the header and backend appends the peers."""
    text = ('# Initial config — panel will overwrite via syncWireguardConfig().\n[Interface]\n' +
            ('Address = 10.66.66.1/24, fd66:66::1/64\n' if address else '') +
            f'ListenPort = 51820\nPrivateKey = {SERVER}\n')
    for key, psk, allowed in sections:
        text += ('\n[Peer]\n' + ('' if key is None else f'PublicKey = {key}'.rstrip() + '\n') +
                 ('' if psk is None else f'PresharedKey = {psk}\n') + f'AllowedIPs = {allowed}\n')
    return text


def wg_reject(text):
    """config.c: the error wg rejects the file with, or None. Only the PublicKey rules."""
    section, keyed = None, True
    for raw in text.split('\n'):
        line = re.sub(r'[ \t\n\v\f\r]', '', raw.split('#', 1)[0])
        if not line:
            continue
        if line.encode().lower() in (b'[interface]', b'[peer]'):
            if section == '[peer]' and not keyed:
                return 'A peer is missing a public key'
            section, keyed = line.lower(), False
            continue
        name, _, value = line.partition('=')
        if section == '[peer]' and name.encode().lower() == b'publickey':
            if not value:
                return f'Line unrecognized: `{line}\''
            keyed = True
    return 'A peer is missing a public key' if section == '[peer]' and not keyed else None


def bundle(directory, text):
    """An unpacked backup bundle: manifest.json, db.sqlite with DEVICES, etc-wireguard/wg0.conf."""
    root = Path(directory)
    (root / 'manifest.json').write_text(json.dumps({'timestamp': '2026-10-30T120000Z'}))
    con = sqlite3.connect(root / 'db.sqlite')
    con.execute('CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, wg_public_key TEXT, '
                'wg_preshared_key TEXT, wg_ip TEXT)')
    con.executemany('INSERT INTO devices VALUES (?, ?, ?, ?, ?)', DEVICES)
    con.commit()
    con.close()
    (root / 'etc-wireguard').mkdir()
    (root / 'etc-wireguard/wg0.conf').write_text(text)
    return root


def backup_check(text):
    """anysda-backup.sh --check-bundle with ANYSDA_LIB_DIR=infra/lib: (exit code, stdout lines)."""
    with tempfile.TemporaryDirectory() as directory:
        done = subprocess.run(['bash', str(LIB / 'anysda-backup.sh'), '--check-bundle', str(bundle(directory, text))],
                              env=os.environ | {'ANYSDA_LIB_DIR': str(LIB)}, capture_output=True, text=True,
                              timeout=60, check=False)
    return done.returncode, done.stdout.splitlines(), done.stderr


def restore_check(text):
    """The check-bundle step of anysda-restore.sh, the script copied alone into a temporary directory.

    The backup script it calls is a copy without wg_conf.py next to it; ANYSDA_LIB_DIR names infra/lib.
    Returns (exit code, stderr).
    """
    block = cut(source('infra/lib/anysda-restore.sh'), 'CHECK=/usr/local/bin/anysda-backup.sh',
                '\n\n# Старые архивы')
    with tempfile.TemporaryDirectory() as directory:
        top = Path(directory)
        for name in ('restore', 'bin', 'work', 'bundle'):
            (top / name).mkdir()
        check = top / 'bin/anysda-backup.sh'
        check.write_text(source('infra/lib/anysda-backup.sh'))
        check.chmod(0o755)
        assert block.count('/usr/local/bin/anysda-backup.sh') == 1, block
        script = top / 'restore/anysda-restore.sh'
        script.write_text('#!/bin/bash\nset -euo pipefail\nBUNDLE_DIR=$1\n' +
                          block.replace('/usr/local/bin/anysda-backup.sh', str(check)) + '\necho CHECKED\n')
        bundle(top / 'bundle', text)
        done = subprocess.run(['bash', str(script), str(top / 'bundle')], cwd=top / 'work',
                              env=os.environ | {'ANYSDA_LIB_DIR': str(LIB)}, capture_output=True, text=True,
                              timeout=60, check=False)
    assert 'CHECKED' in done.stdout, (done.stdout, done.stderr)
    return done.returncode, done.stderr


def installs(text):
    """Every `install` line of a stage script: (line number, destination)."""
    found = []
    for number, line in enumerate(text.split('\n')):
        words = shlex.split(line.split(' #', 1)[0]) if line.strip().startswith('install ') else []
        if words:
            found.append((number, words[words.index('-t') + 1] if '-t' in words else words[-1], words))
    return found


class WgConfModuleTests(unittest.TestCase):
    """1 a [Peer] section without a public key, 2 wg_conf.py in the node's library directory, 0 the model."""

    def test_0_reference_matches_wg(self):
        for form, wanted in WG_ORACLE.items():
            with self.subTest(form):
                self.assertEqual(wg_reject(conf(FORMS[form], address=False)), wanted)
        self.assertIsNone(wg_reject(conf(CANONICAL, address=False)))

    def test_1a_backup_check_fails_on_peer_without_key(self):
        for form in FORMS:
            code, lines, err = backup_check(conf(FORMS[form]))
            with self.subTest('backup exit 3', form=form):
                self.assertEqual(code, 3, (lines, err))
            with self.subTest('backup names PublicKey', form=form):
                self.assertTrue([line for line in lines if 'PublicKey' in line], (lines, err))

    def test_1b_verify_fails_on_peer_without_key(self):
        for form in FORMS:
            text = conf(FORMS[form])
            for name, call in (('conf_peers', VT.conf_peers), ('apply_peers', VT.apply_peers)):
                with self.subTest(f'verify {name} fails', form=form):
                    with self.assertRaises(ValueError) as caught:
                        call(text)
                    self.assertIn('PublicKey', str(caught.exception))
            with self.subTest('verify peers vs wg0: FAIL', form=form):
                notes = apply_gate(text)
                self.assertTrue(notes and notes[0][0] == VT.FAIL and 'PublicKey' in notes[0][1], notes)

    def test_1c_file_as_written_passes(self):
        code, lines, err = backup_check(conf(CANONICAL))
        self.assertEqual((code, lines), (0, []), err)

    def test_2a_stage_26_installs_module_in_node_lib_before_backup(self):
        found = installs(source('infra/scripts/26-backup.sh'))
        module = [(number, target) for number, target, words in found if any('wg_conf.py' in w for w in words)]
        with self.subTest('installed'):
            self.assertTrue(module, found)
        with self.subTest('into /usr/local/lib/anysda'):
            self.assertEqual([t for _, t in module if t.rstrip('/') not in (NODE_LIB, f'{NODE_LIB}/wg_conf.py')], [])
        with self.subTest('not into /usr/local/bin'):
            self.assertEqual([t for _, t in module if t.startswith('/usr/local/bin')], [])
        backup = [number for number, target, _ in found if target == '/usr/local/bin/anysda-backup.sh']
        with self.subTest('before anysda-backup.sh'):
            self.assertTrue(backup and module and min(n for n, _ in module) < min(backup), (module, backup))

    def test_2b_restore_from_any_directory_checks_with_module_from_lib_dir(self):
        code, err = restore_check(conf(CANONICAL))
        with self.subTest('matching bundle: no warning'):
            self.assertEqual((code, err), (0, ''))
        code, err = restore_check(conf(BASE[:2]))
        with self.subTest('peer missing in wg0.conf: names the device'):
            self.assertIn('пиры WireGuard', err)
            self.assertIn('устройство 2 «old»: нет пира WireGuard', err)


if __name__ == '__main__':
    unittest.main()
