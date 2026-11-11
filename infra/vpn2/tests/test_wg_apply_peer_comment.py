"""wg apply: a comment right above the first [Peer] belongs to that peer and survives every apply."""
from pathlib import Path
import tempfile
import unittest

from test_apply import HEADER, KEY, Host, apply

CANDIDATE = HEADER.replace('51820', '51821')
OLD_HEADER = HEADER.replace('51820', '51822')
# Another [Interface] as 28-wireguard.sh writes it, with no comments: the check after it fails.
FAILING = HEADER.replace('51820', '51823')
PEERS = (f'[Peer]\nPublicKey = {KEY["a"]}\nPresharedKey = {KEY["c"]}\nAllowedIPs = 10.66.66.2/32\n'
         f'\n[Peer]\nPublicKey = {KEY["b"]}\nPresharedKey = {KEY["c"]}\nAllowedIPs = 10.66.66.3/32\n')
# The operator's note on the first peer and what follows it up to that [Peer]:
# all of it is the peer's and stays byte for byte.
NOTES = {
    'one line': '# client foo\n',
    'blank line after': '# client foo\n\n',
    'blank lines after': '# client foo\n\n\n',
    'several lines': '# client foo\n# phone, since 2026\n',
    'several lines with a blank between': '# client foo\n\n# phone\n',
}
# What stands between the [Interface] keys and the note; an apply leaves one blank line.
BEFORE = {'no blank line': '', 'one blank line': '\n', 'three blank lines': '\n\n\n', 'spaces and tabs': ' \n\t\n'}


class FailOnce(Host):
    """The first wg0 check after a candidate fails, so the applier restores last good."""

    def __init__(self, root):
        super().__init__(root)
        self.fail = False

    def __call__(self, argv, data=None):
        if self.fail and argv == ['/usr/bin/wg', 'show', 'wg0', 'listen-port']:
            self.fail = False
            raise RuntimeError('wg show: no such device')
        return super().__call__(argv, data)


class WireguardPeerCommentTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        (self.work / 'state').mkdir()
        self.host = FailOnce(self.work / 'root')
        self.applier = apply.Applier(self.work / 'root', self.work / 'state', self.host)
        self.applier.settle = 0
        self.path = self.work / 'root/etc/wireguard/wg0.conf'
        self.path.parent.mkdir(parents=True)
        self.new = self.work / 'new.conf'
        self.candidate(CANDIDATE)

    def candidate(self, text):
        self.new.write_text(text)

    def live(self, text):
        self.path.write_text(text)
        self.applier.good('wg').unlink(missing_ok=True)

    def install(self):
        self.applier.install('wg', self.new)
        return self.path.read_text()

    def rollback(self):
        with self.assertRaises(apply.Failed) as caught:
            self.applier.rollback('wg')
        self.assertEqual(caught.exception.code, 3, str(caught.exception))
        return self.path.read_text()

    def failed_install(self):
        self.host.fail = True
        with self.assertRaises(apply.Failed) as caught:
            self.applier.install('wg', self.new)
        self.assertEqual(caught.exception.code, 3, str(caught.exception))
        return self.path.read_text()

    def assertThreeTimes(self, step, expected):
        for attempt in (1, 2, 3):
            self.assertEqual(step(), expected, f'after apply {attempt}')

    def test_note_on_first_peer_survives_three_applies(self):
        for before_name, before in BEFORE.items():
            for name, note in NOTES.items():
                with self.subTest(name, before=before_name):
                    self.live(OLD_HEADER + before + note + PEERS)
                    self.assertThreeTimes(self.install, CANDIDATE + '\n' + note + PEERS)

    def test_note_on_first_peer_survives_a_hand_edited_tail(self):
        tail = f'[Peer]\nPublicKey = {KEY["b"]}\nAllowedIPs = 10.66.66.3/32\n\n\n# laptop\n\n[Peer]\nPublicKey = {KEY["a"]}'
        self.live(OLD_HEADER + '\n# client foo\n' + tail)
        self.assertThreeTimes(self.install, CANDIDATE + '\n# client foo\n' + tail)

    def test_note_without_peers_goes_with_the_interface(self):
        # No [Peer] follows, so the comment signs nothing: it is part of the old [Interface].
        for name, note in NOTES.items():
            with self.subTest(name):
                self.live(OLD_HEADER + '\n' + note)
                self.assertThreeTimes(self.install, CANDIDATE)

    def test_restore_of_last_good_keeps_one_note(self):
        for before_name, before in BEFORE.items():
            for name, note in NOTES.items():
                expected = OLD_HEADER + '\n' + note + PEERS
                with self.subTest(name, before=before_name, path='rollback'):
                    self.live(OLD_HEADER + before + note + PEERS)
                    self.applier.mark('wg')
                    self.assertThreeTimes(self.rollback, expected)
                with self.subTest(name, before=before_name, path='candidate fails, last good restored'):
                    self.live(OLD_HEADER + before + note + PEERS)
                    self.applier.mark('wg')
                    self.assertThreeTimes(self.failed_install, expected)

    def test_failed_apply_after_a_good_one_keeps_one_note(self):
        for name, note in NOTES.items():
            with self.subTest(name):
                self.live(OLD_HEADER + '\n' + note + PEERS)
                installed = self.install()
                self.assertEqual(installed, CANDIDATE + '\n' + note + PEERS)
                self.candidate(FAILING)
                self.assertThreeTimes(self.failed_install, installed)
                self.candidate(CANDIDATE)


if __name__ == '__main__':
    unittest.main()
