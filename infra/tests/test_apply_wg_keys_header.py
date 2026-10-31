"""wg candidate check: the section of a wg-quick key is the one wg-quick reads.

wg reads a section header with every blank thrown out, so "[ Interface ]" and
"[Inter face]" are [Interface] to it. wg-quick takes Address and its other keys
out only under a line that, trimmed at both ends, is [Interface] in any ASCII
case. Under "[ Interface ]" or "[Inter face]" it gives Address to wg, wg says
"Line unrecognized" and the next wg-quick up fails. The install refuses such a
candidate with code 2 and leaves the working wg0.conf as it was. The oracle is
wg-quick and wg themselves, as in test_wg_conf_wg_oracle.
"""
from pathlib import Path
import tempfile
import unittest

from test_apply import HEADER, apply, peer
from test_apply_wg_keys import Host
from test_wg_conf_wg_oracle import IP, WG, WG_QUICK, oracle
from wg_root import as_root

# Headers wg reads as [Interface] and wg-quick does not: Address goes to wg.
REJECTED = {'blanks-inside-brackets': '[ Interface ]', 'blank-inside-name': '[Inter face]'}
# Headers both read as [Interface]: wg-quick takes Address out.
LEGAL = {'plain': '[Interface]', 'lowercase': '[interface]', 'blanks-around': '  [Interface]  '}
PEER = peer('a')


def candidate(header):
    return HEADER.replace('[Interface]', header, 1)


class WireguardHeaderTests(unittest.TestCase):
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

    def test_header_wg_quick_does_not_read_is_rejected(self):
        working = (HEADER + PEER).encode()
        for name, header in REJECTED.items():
            with self.subTest(name=name):
                self.path.write_bytes(working)
                self.host.calls.clear()
                self.new.write_text(candidate(header))
                with self.assertRaises(apply.Failed) as caught:
                    self.applier.install('wg', self.new)
                self.assertEqual(caught.exception.code, 2, str(caught.exception))
                self.assertIn('candidate rejected, working config untouched', str(caught.exception))
                self.assertEqual(self.synced(), [])
                self.assertEqual(self.path.read_bytes(), working)
                self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), ['wg0.conf'])

    def test_header_both_read_still_passes(self):
        for name, header in LEGAL.items():
            with self.subTest(name=name):
                self.path.write_text(HEADER + PEER)
                self.host.calls.clear()
                self.new.write_text(candidate(header))
                self.applier.install('wg', self.new)
                self.assertEqual(len(self.synced()), 1)
                self.assertIn('Address = 10.66.66.1/24', self.path.read_text().split('\n'))
                self.assertTrue(self.path.read_text().endswith(PEER))


@as_root
@unittest.skipUnless(WG and WG_QUICK and IP, 'needs wg, wg-quick and ip')
class WireguardHeaderOracleTests(unittest.TestCase):
    """The headers above, asked of wg-quick strip and wg setconf on the whole wg0.conf."""

    def test_wg_rejects_address_under_header_wg_quick_does_not_read(self):
        for name, header in REJECTED.items():
            with self.subTest(name=name):
                error = oracle((candidate(header) + PEER).encode())[0]
                self.assertIsNotNone(error)
                self.assertIn('Line unrecognized', error)

    def test_wg_takes_address_under_header_both_read(self):
        for name, header in LEGAL.items():
            with self.subTest(name=name):
                self.assertIsNone(oracle((candidate(header) + PEER).encode())[0])


if __name__ == '__main__':
    unittest.main()
