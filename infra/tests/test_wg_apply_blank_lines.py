"""wg apply: the same [Interface] applied again leaves wg0.conf byte for byte as after the first time."""
from pathlib import Path
import tempfile
import unittest

from test_apply import HEADER, KEY, Host, apply

CANDIDATE = HEADER.replace('51820', '51821')
OLD_HEADER = HEADER.replace('51820', '51822')
# Peers as the control backend writes them: each after one blank line, PresharedKey included.
PEERS = (f'[Peer]\nPublicKey = {KEY["a"]}\nPresharedKey = {KEY["c"]}\nAllowedIPs = 10.66.66.2/32\n'
         f'\n[Peer]\nPublicKey = {KEY["b"]}\nPresharedKey = {KEY["c"]}\nAllowedIPs = 10.66.66.3/32\n')
# A tail edited by hand: comments, several blank lines between peers, no newline at the end.
EDITED = (f'[Peer]\n# phone\nPublicKey = {KEY["b"]}\nAllowedIPs = 10.66.66.3/32\n\n\n'
          f'# laptop\n\n[Peer]\nPublicKey = {KEY["a"]}\nPresharedKey = {KEY["c"]}\nAllowedIPs = 10.66.66.2/32')
LIVE = {
    'one blank line': OLD_HEADER + '\n' + PEERS,
    'three blank lines': OLD_HEADER + '\n\n\n' + PEERS,
    'spaces and tabs': OLD_HEADER + ' \n\t\n \t \n' + PEERS,
    'no peers': OLD_HEADER,
    'no peers, no newline at the end': OLD_HEADER.rstrip('\n'),
    'peers, no newline at the end': OLD_HEADER + '\n' + PEERS.rstrip('\n'),
    'edited tail': OLD_HEADER + '\n' + EDITED,
}


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


class WireguardBlankLinesTests(unittest.TestCase):
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
        self.new.write_text(CANDIDATE)

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

    def synced(self):
        return [data for argv, data in self.host.calls if argv[:2] == ['/usr/bin/wg', 'syncconf']]

    def assertStable(self, step):
        first = step()
        for attempt in (2, 3):
            self.assertEqual(step(), first, f'after apply {attempt}')
        return first

    def test_three_applies_of_one_candidate_give_one_file(self):
        for name, text in LIVE.items():
            with self.subTest(name):
                self.live(text)
                self.assertTrue(self.assertStable(self.install).startswith(CANDIDATE))

    def test_restore_of_last_good_three_times_gives_one_file(self):
        for name, text in LIVE.items():
            with self.subTest(name, path='rollback'):
                self.live(text)
                self.applier.mark('wg')
                self.assertTrue(self.assertStable(self.rollback).startswith(OLD_HEADER))

    def test_candidate_failing_three_times_restores_one_file(self):
        for name, text in LIVE.items():
            with self.subTest(name, path='candidate fails, last good restored'):
                self.live(text)
                self.applier.mark('wg')
                self.assertTrue(self.assertStable(self.failed_install).startswith(OLD_HEADER))

    def test_grown_file_is_brought_back_to_one_blank_line(self):
        self.live(OLD_HEADER + '\n' * 5 + PEERS)
        self.assertEqual(self.install(), CANDIDATE + '\n' + PEERS)
        self.assertEqual(self.install(), CANDIDATE + '\n' + PEERS)

    def test_peer_tail_is_kept_byte_for_byte(self):
        for name, tail in (('backend', PEERS), ('edited', EDITED)):
            for blank in ('\n', '\n\n\n', ' \n\t\n'):
                with self.subTest(name, blank=ascii(blank)):
                    self.live(OLD_HEADER + blank + tail)
                    self.assertEqual(self.install(), CANDIDATE + '\n' + tail)

    def test_syncconf_lines_do_not_change(self):
        interface = f'[Interface]\nListenPort = 51821\nPrivateKey = {KEY["s"]}\n'
        expected = {
            'backend': interface + f'[Peer]\nPublicKey = {KEY["a"]}\nPresharedKey = {KEY["c"]}\n'
                                   f'AllowedIPs = 10.66.66.2/32\n[Peer]\nPublicKey = {KEY["b"]}\n'
                                   f'PresharedKey = {KEY["c"]}\nAllowedIPs = 10.66.66.3/32\n',
            'edited': interface + f'[Peer]\nPublicKey = {KEY["b"]}\nAllowedIPs = 10.66.66.3/32\n'
                                  f'[Peer]\nPublicKey = {KEY["a"]}\nPresharedKey = {KEY["c"]}\n'
                                  f'AllowedIPs = 10.66.66.2/32\n',
        }
        for name, tail in (('backend', PEERS), ('edited', EDITED)):
            with self.subTest(name):
                self.live(OLD_HEADER + '\n' + tail)
                self.host.calls.clear()
                for _ in range(3):
                    self.install()
                self.assertEqual(self.synced(), [expected[name]] * 3)


if __name__ == '__main__':
    unittest.main()
