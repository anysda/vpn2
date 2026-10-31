"""wg apply: a comment that ends the candidate's [Interface] is written once, however many applies."""
from pathlib import Path
import tempfile
import unittest

from test_apply import HEADER, KEY, Host, apply

CANDIDATE = HEADER.replace('51820', '51821')
OLD_HEADER = HEADER.replace('51820', '51822')
PEERS = (f'[Peer]\nPublicKey = {KEY["a"]}\nPresharedKey = {KEY["c"]}\nAllowedIPs = 10.66.66.2/32\n'
         f'\n[Peer]\nPublicKey = {KEY["b"]}\nPresharedKey = {KEY["c"]}\nAllowedIPs = 10.66.66.3/32\n')
# What the live file has right above its first [Peer].
LIVE_NOTES = {'no note': '', 'note on the first peer': '# client foo\n'}


class WireguardHeadCommentTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        (self.work / 'state').mkdir()
        self.applier = apply.Applier(self.work / 'root', self.work / 'state', Host(self.work / 'root'))
        self.applier.settle = 0
        self.path = self.work / 'root/etc/wireguard/wg0.conf'
        self.path.parent.mkdir(parents=True)
        self.new = self.work / 'new.conf'

    def installs(self, live, candidate):
        self.path.write_text(live)
        self.applier.good('wg').unlink(missing_ok=True)
        self.new.write_text(candidate)
        results = []
        for _ in range(3):
            self.applier.install('wg', self.new)
            results.append(self.path.read_text())
        return results

    def assertStable(self, results):
        self.assertEqual(results[1], results[0], 'after apply 2')
        self.assertEqual(results[2], results[0], 'after apply 3')

    def test_comment_ending_the_candidate_interface_is_written_once(self):
        for name, note in LIVE_NOTES.items():
            with self.subTest(name):
                results = self.installs(OLD_HEADER + '\n' + note + PEERS, CANDIDATE + '# managed by ops\n')
                self.assertEqual(results[0].count('# managed by ops'), 1)
                self.assertIn(note + PEERS, results[0])
                self.assertStable(results)

    def test_candidate_cut_from_the_live_file_installs_byte_for_byte(self):
        # sed '/^\[Peer\]/,$d' wg0.conf > new.conf: the note on the first peer ends the candidate.
        for name, note in LIVE_NOTES.items():
            with self.subTest(name):
                live = OLD_HEADER + '\n' + note + PEERS
                results = self.installs(live, live[:live.index('[Peer]')].replace('51822', '51821'))
                self.assertEqual(results[0], CANDIDATE + '\n' + note + PEERS)
                self.assertStable(results)


if __name__ == '__main__':
    unittest.main()
