"""wg0.conf whose sections or keys are where apply cannot keep them: rejected before syncconf.

A key outside a section and ListenPort or PrivateKey in a [Peer] fail wg itself
(wireguard-tools config.c, process_line: "Line unrecognized"). A second
[Interface] after a [Peer] wg reads, but apply replaces the first [Interface]
whole and would drop or misplace the later one, so it is refused too.
"""
from pathlib import Path
import tempfile
import unittest

from test_apply import HEADER, KEY, apply, peer
from test_apply_wg_keys import Host


class SectionOrderTests(unittest.TestCase):
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

    def rejected(self, current, candidate=HEADER):
        """The message of an install that must stop with code 2 before syncconf."""
        self.path.write_text(current)
        self.host.calls.clear()
        self.new.write_text(candidate)
        with self.assertRaises(apply.Failed) as caught:
            self.applier.install('wg', self.new)
        self.assertEqual(caught.exception.code, 2, str(caught.exception))
        self.assertIn('candidate rejected, working config untouched', str(caught.exception))
        self.assertEqual([argv for argv, _ in self.host.calls if argv[:2] == ['/usr/bin/wg', 'syncconf']], [])
        self.assertEqual(self.path.read_text(), current)
        return str(caught.exception)

    def test_interface_after_a_peer_is_rejected(self):
        for later in ('\n[Interface]\nFwMark = 0x10\n', '\n[Interface]\n', '\n[ interface ] # second\nFwMark = 7\n'):
            for tail in ('', peer('b')):
                with self.subTest(later=later, tail=tail):
                    message = self.rejected(HEADER + peer('a') + later + tail)
                    self.assertIn('/etc/wireguard/wg0.conf', message)
                    self.assertIn('after [Peer] number 1', message)
                    self.assertIn('first [Interface]', message)

    def test_key_before_the_first_interface_is_rejected(self):
        for key in ('ListenPort = 51820', f'PrivateKey = {KEY["s"]}', 'FwMark = 1'):
            with self.subTest(key=key):
                self.rejected(HEADER + peer('a'), key + '\n' + HEADER)

    def test_interface_key_in_a_peer_is_rejected(self):
        for key in ('ListenPort = 51821', f'PrivateKey = {KEY["b"]}', 'fwmark = 1'):
            for where in ('a', 'b'):
                current = HEADER + peer('a') + peer('b')
                current = current.replace(f'PublicKey = {KEY[where]}\n', f'PublicKey = {KEY[where]}\n{key}\n')
                with self.subTest(key=key, peer=where):
                    self.rejected(current)

    def test_well_ordered_config_is_applied(self):
        self.path.write_text(HEADER + peer('a') + peer('b'))
        self.new.write_text(HEADER)
        self.applier.install('wg', self.new)
        synced = [data for argv, data in self.host.calls if argv[:2] == ['/usr/bin/wg', 'syncconf']]
        self.assertEqual(len(synced), 1)
        self.assertEqual(synced[0].count('[Peer]'), 2)


if __name__ == '__main__':
    unittest.main()
