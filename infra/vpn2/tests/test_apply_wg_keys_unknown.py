"""wg candidate check: a line wg-quick strip leaves and wg setconf does not know is refused.

wg-quick takes Address, DNS, MTU, Table, PreUp/PostUp/PreDown/PostDown and
SaveConfig out of [Interface] (bash nocasematch, the name trimmed at both ends
only) and gives every other line to wg. wg reads only its own keys, each in its
section: a misspelt name, a name with a letter outside ASCII, a name with a
blank inside or a [Peer] key under [Interface] is "Line unrecognized", and the
next wg-quick up fails on it. The install refuses such a candidate with code 2
and leaves the working wg0.conf as it was. The oracle is wg-quick and wg
themselves (wireguard-tools 1.0.20250521), as in test_wg_conf_wg_oracle.
"""
from pathlib import Path
import tempfile
import unittest

from test_apply import HEADER, apply, peer
from test_apply_wg_keys import Host
from wg_oracle import IP, WG, WG_QUICK, as_root, oracle

# Lines in [Interface] wg-quick gives to wg and wg does not read.
UNKNOWN = {
    'typo-address': 'Adress = 10.66.66.1/24',
    'typo-endpoint': 'Endpiont = 1.2.3.4:51820',
    'typo-mtu': 'MTUU = 1420',
    'long-s-address': 'Addreſs = 10.66.66.1/24',
    'long-s-allowedips': 'AllowedIPſ = 10.66.66.0/24',
    'long-s-presharedkey': 'PreſharedKey = ' + 'c' * 42 + 'A=',
    'dotless-i-saveconfig': 'SaveConfıg = false',
    'dotted-i-saveconfig': 'SaveConfİg = false',
    'blank-inside-postup': 'Post Up = true',
    'peer-key-endpoint': 'Endpoint = 1.2.3.4:51820',
    'peer-key-keepalive': 'PersistentKeepalive = 25',
}
# Sections wg does not read: their keys are outside any section.
SECTIONS = {'dotted-i-section': '[İnterface]', 'dotless-i-section': '[ınterface]'}
# Lines wg-quick takes out of [Interface], in any ASCII case and with blanks around.
LEGAL = ('Address = 10.66.66.1/24, fd66:66::1/64', 'DNS = 1.1.1.1', 'MTU = 1420', 'Table = off',
         'PreUp = true', 'PostUp = true', 'PreDown = true', 'PostDown = true', 'SaveConfig = false',
         'mtu = 1420', 'POSTDOWN = true', '\tdns\t=\t1.1.1.1', '# Adress = 10.66.66.1/24')
PEER = peer('a') + 'Endpoint = 1.2.3.4:51820\nPersistentKeepalive = 25\n'


def candidates():
    found = {name: HEADER + line + '\n' for name, line in UNKNOWN.items()}
    found |= {name: HEADER.replace('[Interface]', section) for name, section in SECTIONS.items()}
    return found


class WireguardUnknownKeyTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        (self.work / 'state').mkdir()
        self.host = Host(self.work)
        self.applier = apply.Applier(self.work / 'root', self.work / 'state', self.host)
        self.applier.settle = 0
        self.path = self.work / 'root/etc/wireguard/wg0.conf'
        self.path.parent.mkdir(parents=True)
        self.new = self.work / 'new.conf'

    def synced(self):
        return [data for argv, data in self.host.calls if argv[:2] == ['/usr/bin/wg', 'syncconf']]

    def test_line_wg_does_not_know_is_rejected(self):
        working = (HEADER + PEER).encode()
        for name, text in candidates().items():
            with self.subTest(name=name):
                self.path.write_bytes(working)
                self.host.calls.clear()
                self.new.write_text(text, encoding='utf-8')
                with self.assertRaises(apply.Failed) as caught:
                    self.applier.install('wg', self.new)
                self.assertEqual(caught.exception.code, 2, str(caught.exception))
                self.assertIn('candidate rejected, working config untouched', str(caught.exception))
                self.assertEqual(self.synced(), [])
                self.assertEqual(self.path.read_bytes(), working)
                self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), ['wg0.conf'])

    def test_wg_quick_and_wg_keys_still_pass(self):
        for line in LEGAL:
            with self.subTest(line=line):
                self.path.write_text(HEADER + PEER)
                self.host.calls.clear()
                self.new.write_text(HEADER + line + '\n')
                self.applier.install('wg', self.new)
                self.assertEqual(len(self.synced()), 1)
                self.assertIn(line, self.path.read_text().split('\n'))
                self.assertTrue(self.path.read_text().endswith(PEER))


@as_root
@unittest.skipUnless(WG and WG_QUICK and IP, 'needs wg, wg-quick and ip')
class WireguardUnknownKeyOracleTests(unittest.TestCase):
    """The lists above, asked of wg-quick strip and wg setconf on the whole wg0.conf."""

    def test_wg_rejects_every_unknown_line(self):
        for name, text in candidates().items():
            with self.subTest(name=name):
                error = oracle((text + PEER).encode())[0]
                self.assertIsNotNone(error)
                self.assertIn('Line unrecognized', error)

    def test_wg_takes_every_legal_line(self):
        for line in LEGAL:
            with self.subTest(line=line):
                self.assertIsNone(oracle((HEADER + line + '\n' + PEER).encode())[0])


if __name__ == '__main__':
    unittest.main()
