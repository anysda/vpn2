"""wg0.conf outside apply: a line wg does not recognize, the backup script's wg_conf.py on the node.

wg rejects the whole file on a line it does not recognize: an unknown name, a
typo in a name, a line without `=`, an unknown section, a name with a
non-ASCII letter next to the real one. wg-quick takes out only its own names
in [Interface] (Address, DNS, MTU, Table, PreUp, PostUp, PreDown, PostDown,
SaveConfig) before wg. The backup check (anysda-backup.sh --check-bundle)
must fail on such a file like wg does, not read past the line. On the node the backup script lies in /usr/local/bin and takes
wg_conf.py from /usr/local/lib/anysda, not from a wg_conf.py next to itself.
"""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vpn2/tests'))
from test_control import module  # noqa: E402
from test_wg_conf_allowedips_prefix import NEW, PSK_TWO, ROOT, SERVER
from test_wg_conf_module import BASE, DEVICES, backup_check, conf
import vpn_roles
from wg_root import as_root

WG_QUICK = ('Address', 'DNS', 'MTU', 'Table', 'PreUp', 'PostUp', 'PreDown', 'PostDown', 'SaveConfig')
WG_NAMES = {'[interface]': {'privatekey', 'listenport', 'fwmark'},
            '[peer]': {'publickey', 'presharedkey', 'allowedips', 'endpoint', 'persistentkeepalive'}}
# Lines wg does not recognize, put into [Interface] after PrivateKey or into the last [Peer].
UNRECOGNIZED = {
    'interface': {
        'unknown-key': 'Foo = 1',
        'typo': 'ListenPrt = 51821',
        'no-equals': 'garbage',
        'unknown-section': '[Interfaces]\nListenPort = 51821',
        'non-ascii-name': f'PrivateKеy = {SERVER}',
    },
    'peer': {
        'unknown-key': 'Foo = 1',
        'typo': 'AllowedIP = 10.66.66.9/32',
        'no-equals': 'garbage',
        'unknown-section': f'[Peers]\nPublicKey = {NEW}\nAllowedIPs = 10.66.66.9/32',
        'non-ascii-name': f'PublicKеy = {NEW}',
    },
}
# Lines wg-quick and wg accept, put the same way.
ACCEPTED = {
    'interface': {
        'wg-quick-names': '\n'.join(f'{name} = {value}' for name, value in zip(WG_QUICK, (
            '10.66.66.1/24', '1.1.1.1', '1420', 'off', 'true', 'true', 'true', 'true', 'false'))),
        'comments-and-blank-lines': '# note\n   # indented note\n\n \t \nFwMark = 0 # mark',
    },
    'peer': {
        'comments-and-blank-lines': f'# note\n\n\t\nPresharedKey = {PSK_TWO} # psk',
    },
}
# `wg setconf wg0 /dev/stdin` of each form with the wg-quick names taken out, on a fresh wg0 in a netns
# (Linux 7.0, wireguard-tools 1.0.20250521): the error wg rejects the whole file with, None if it applies.
WG_ORACLE = {
    ('interface', 'unknown-key'): 'Line unrecognized: `Foo=1\'',
    ('interface', 'typo'): 'Line unrecognized: `ListenPrt=51821\'',
    ('interface', 'no-equals'): 'Line unrecognized: `garbage\'',
    ('interface', 'unknown-section'): 'Line unrecognized: `[Interfaces]\'',
    ('interface', 'non-ascii-name'): f'Line unrecognized: `PrivateKеy={SERVER}\'',
    ('peer', 'unknown-key'): 'Line unrecognized: `Foo=1\'',
    ('peer', 'typo'): 'Line unrecognized: `AllowedIP=10.66.66.9/32\'',
    ('peer', 'no-equals'): 'Line unrecognized: `garbage\'',
    ('peer', 'unknown-section'): 'Line unrecognized: `[Peers]\'',
    ('peer', 'non-ascii-name'): f'Line unrecognized: `PublicKеy={NEW}\'',
    ('interface', 'wg-quick-names'): None,
    ('interface', 'comments-and-blank-lines'): None,
    ('peer', 'comments-and-blank-lines'): None,
    ('written', 'written'): None,
}


def with_lines(place, lines):
    """conf(BASE) with the lines after PrivateKey of [Interface] or at the end of the last [Peer]."""
    text = conf(BASE)
    if place == 'peer':
        return text + lines + '\n'
    line = f'PrivateKey = {SERVER}\n'
    assert text.count(line) == 1, text
    return text.replace(line, line + lines + '\n')


def written():
    """wg0.conf as the node has it: the header the wireguard role writes, the peers backend wg.apply writes."""
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / 'server.priv').write_text(SERVER + '\n')
        done = vpn_roles.wg_identity(directory, stub=f'wg() {{ cat >/dev/null; echo {SERVER}; }}\n')
        assert not done.returncode, done.stderr
        header = (Path(directory) / 'wg0.conf').read_text()
    module('contract')
    peers = [{'key': key, 'preshared_key': psk, 'ip': ip} for _, _, key, psk, ip in DEVICES]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'state').mkdir()
        (root / 'etc/wireguard').mkdir(parents=True)
        (root / 'etc/wireguard/wg0.conf').write_text(header)
        backend = module('backend').Backend(root, root / 'state')
        backend.pools = json.loads((ROOT / 'app/shared/vpn-pools.json').read_text())

        def command(*args, data=None):
            del data
            if args[-1] == 'preshared-keys':
                return ''.join(f'{p["key"]}\t{p["preshared_key"]}\n' for p in peers)
            if args[-1] == 'allowed-ips':
                return ''.join(f'{p["key"]}\t{p["ip"]}/32 fd66:66::{p["ip"].split(".")[-1]}/128\n' for p in peers)
            return ''

        with patch.object(backend, 'command', side_effect=command):
            backend.wireguard({'version': 1, 'peers': peers})
        return (root / 'etc/wireguard/wg0.conf').read_text()


def forms():
    """(place, form) -> wg0.conf text of every form above and the file as written."""
    found = {(place, form): with_lines(place, lines)
             for table in (UNRECOGNIZED, ACCEPTED) for place, lines_of in table.items()
             for form, lines in lines_of.items()}
    found['written', 'written'] = written()
    return found


def wg_quick_strip(text):
    """The text wg-quick hands to wg (parse_options): its own names taken out, any case.

    The key is the line before `#` and the first `=`, trimmed only at its ends; a key
    starting with `[` ends the section, and only the key `[Interface]` as a whole starts
    the one where wg-quick takes its names out. `[ Interface ]` and `Post Up` stay for wg.
    """
    kept, inside = [], False
    for raw in text.split('\n'):
        key = raw.split('#', 1)[0].split('=', 1)[0].strip(' \t\n\v\f\r')
        if key.startswith('['):
            inside = key.encode().lower() == b'[interface]'
        elif inside and key.encode().lower() in {n.lower().encode() for n in WG_QUICK}:
            continue
        kept.append(raw)
    return '\n'.join(kept)


def wg_reject(text):
    """config.c: the `Line unrecognized` error wg rejects the file with, or None.

    A name matches only with a value after `=` (get_value: keylen >= linelen is no match).
    """
    section = None
    for raw in text.split('\n'):
        line = re.sub(r'[ \t\n\v\f\r]', '', raw.split('#', 1)[0])
        if not line:
            continue
        lower = line.encode().lower()
        if line.startswith('['):
            if lower not in (b'[interface]', b'[peer]'):
                return f'Line unrecognized: `{line}\''
            section = lower.decode()
            continue
        name, eq, value = line.partition('=')
        if not eq or not value or section is None or \
                name.encode().lower() not in {n.encode() for n in WG_NAMES[section]}:
            return f'Line unrecognized: `{line}\''
    return None




@as_root
class WgConfLinesTests(unittest.TestCase):
    """1 lines wg does not recognize, 2 wg_conf.py from the node's library directory, 0 the model."""

    def test_0_reference_matches_wg(self):
        for (place, form), text in forms().items():
            with self.subTest(place=place, form=form):
                self.assertEqual(wg_reject(wg_quick_strip(text)), WG_ORACLE[place, form])

    def test_1a_backup_check_fails_on_unrecognized_line(self):
        for place, lines_of in UNRECOGNIZED.items():
            for form, lines in lines_of.items():
                code, out, err = backup_check(with_lines(place, lines))
                with self.subTest('backup exit 3', place=place, form=form):
                    self.assertEqual(code, 3, (out, err))
                with self.subTest('backup names wg0.conf', place=place, form=form):
                    self.assertTrue([line for line in out if 'wg0.conf' in line], (out, err))

    def test_1c_accepted_lines_and_file_as_written_pass(self):
        for (place, form), text in forms().items():
            if WG_ORACLE[place, form] is not None:
                continue
            with self.subTest('backup: no complaint', place=place, form=form):
                code, out, err = backup_check(text)
                self.assertEqual((code, out), (0, []), err)

if __name__ == '__main__':
    unittest.main()
