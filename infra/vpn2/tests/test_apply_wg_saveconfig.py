"""wg candidate check: SaveConfig in [Interface] is true or false as wg-quick reads it.

wg-quick takes SaveConfig out of [Interface] and hands its value to read_bool:
the value after the first "=", cut at "#" and trimmed of [[:space:]] at both
ends, must be true or false in any case (bash nocasematch). Anything else,
an empty value or a line with no "=" at all, stops wg-quick strip and up with
"`maybe' is neither true nor false". The install refuses such a candidate with
code 2 and leaves the working wg0.conf as it was. The oracle is wg-quick and
wg themselves, as in test_wg_conf_wg_oracle.
"""
from pathlib import Path
import tempfile
import unittest

from test_apply import HEADER, apply, peer
from test_apply_wg_keys import Host
from wg_oracle import IP, WG, WG_QUICK, as_root, oracle

# SaveConfig lines wg-quick's read_bool refuses.
REJECTED = {'maybe': 'SaveConfig = maybe', 'empty': 'SaveConfig =', 'one': 'SaveConfig = 1',
            'yes': 'SaveConfig = yes', 'quoted': 'SaveConfig = "true"', 'no-equals': 'SaveConfig'}
# SaveConfig lines it takes: true or false in any case, blanks and a comment around.
LEGAL = ('SaveConfig = true', 'SaveConfig = false', 'SaveConfig = TRUE', 'saveconfig = False',
         'SaveConfig=true', 'SaveConfig =\t true \t', 'SaveConfig = false # keep peers')
PEER = peer('a')


class WireguardSaveConfigTests(unittest.TestCase):
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

    def test_saveconfig_wg_quick_refuses_is_rejected(self):
        working = (HEADER + PEER).encode()
        for name, line in REJECTED.items():
            with self.subTest(name=name):
                self.path.write_bytes(working)
                self.host.calls.clear()
                self.new.write_text(HEADER + line + '\n')
                with self.assertRaises(apply.Failed) as caught:
                    self.applier.install('wg', self.new)
                self.assertEqual(caught.exception.code, 2, str(caught.exception))
                self.assertIn('candidate rejected, working config untouched', str(caught.exception))
                self.assertEqual(self.synced(), [])
                self.assertEqual(self.path.read_bytes(), working)
                self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), ['wg0.conf'])

    def test_saveconfig_true_or_false_still_passes(self):
        for line in LEGAL:
            with self.subTest(line=line):
                self.path.write_text(HEADER + PEER)
                self.host.calls.clear()
                self.new.write_text(HEADER + line + '\n')
                self.applier.install('wg', self.new)
                self.assertEqual(len(self.synced()), 1)
                # The install drops blanks at the end of a line, as wg-quick does.
                self.assertIn(line.rstrip(' \t'), self.path.read_text().split('\n'))
                self.assertTrue(self.path.read_text().endswith(PEER))


@as_root
@unittest.skipUnless(WG and WG_QUICK and IP, 'needs wg, wg-quick and ip')
class WireguardSaveConfigOracleTests(unittest.TestCase):
    """The lines above, asked of wg-quick strip and wg setconf on the whole wg0.conf."""

    def test_wg_quick_refuses_every_rejected_value(self):
        for name, line in REJECTED.items():
            with self.subTest(name=name):
                error = oracle((HEADER + line + '\n' + PEER).encode())[0]
                self.assertIsNotNone(error)
                self.assertIn('is neither true nor false', error)

    def test_wg_quick_takes_every_legal_value(self):
        for line in LEGAL:
            with self.subTest(line=line):
                self.assertIsNone(oracle((HEADER + line + '\n' + PEER).encode())[0])


if __name__ == '__main__':
    unittest.main()
