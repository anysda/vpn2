"""OpenVPN cipher check: a second signal leaves nothing either; last good comes back where no server can start."""
from pathlib import Path
import unittest
from unittest import mock

from test_apply import apply
import test_apply_openvpn_candidate as candidate
import test_apply_openvpn_check_env as env
from test_apply_openvpn_check_signal import Stopped

OPENVPN = candidate.OPENVPN
WORKING = candidate.WORKING
GOOD = env.GOOD


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnSecondSignalTests(Stopped, unittest.TestCase):
    setUp_tree = candidate.OpenvpnCandidateTests.setUp

    def test_second_signal_while_apply_stops_leaves_nothing(self):
        # The first signal decides the code; one that comes while apply cleans up does not cut it short.
        self.each([], [({'signals': f'{first}+{second}', 'deaf_to_sigterm': deaf},
                        ([(2, first), (0.05, second)], 'kill', deaf))
                       for deaf in (False, True)
                       for first, second in (('SIGINT', 'SIGINT'), ('SIGTERM', 'SIGINT'),
                                             ('SIGHUP', 'SIGINT'), ('SIGTERM', 'SIGTERM'))])


# Last good was accepted once; where no check server can start it still comes back, and a candidate is decided.
@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnLastGoodWithoutServerTests(env.Environments, unittest.TestCase):
    setUp = candidate.OpenvpnCandidateTests.setUp

    def each(self, test):
        for name, swap, euid in self.environments():
            with self.subTest(environment=name):
                # Each environment from a last good the earlier one did not leave half restored.
                self.applier.journal('openvpn').unlink(missing_ok=True)
                (self.base / 'server.conf').write_text(GOOD)
                self.applier.mark('openvpn')
                self.host.calls.clear()
                with env.substituted(swap, euid):
                    test()

    def assertRestored(self, failure):
        self.assertEqual(failure.code, 3, failure)
        self.assertIn('previous config restored', str(failure))
        self.assertNotRegex(str(failure), '[А-Яа-яЁё]')
        self.assertEqual((self.base / 'server.conf').read_text(), GOOD)
        journal = self.applier.journal('openvpn')
        self.assertFalse(journal.exists(), journal.exists() and journal.read_text())
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ['ccd', 'pki', 'server.conf'])

    def test_failed_apply_restores_last_good(self):
        def test():
            # The fake host keeps a unit with BROKEN in its config down after the restart.
            self.new.write_text(WORKING + '# BROKEN\n')
            with self.assertRaises(apply.Failed) as caught:
                self.applier.install('openvpn', self.new)
            self.assertRestored(caught.exception)
        self.each(test)

    def test_rollback_restores_last_good(self):
        def test():
            (self.base / 'server.conf').write_text(WORKING + '# BROKEN\n')
            with self.assertRaises(apply.Failed) as caught:
                self.applier.rollback('openvpn')
            self.assertRestored(caught.exception)
        self.each(test)

    def test_candidate_with_a_known_cipher_is_applied(self):
        def test():
            self.new.write_text(env.KNOWN)
            self.applier.install('openvpn', self.new)
            self.assertEqual((self.base / 'server.conf').read_text(), env.KNOWN)
            self.assertEqual(len(self.host.ran('restart')), 1)
        self.each(test)


# A host or container without a tun device skips the oracle, it does not fail there.
class ServerConditionTests(unittest.TestCase):
    def test_no_tun_means_no_oracle(self):
        real = candidate.subprocess.run

        def run(argv, *args, **kwargs):
            if 'tuntap' in argv or '/dev/net/tun' in ' '.join(map(str, argv)):
                return candidate.subprocess.CompletedProcess(argv, 1, b'', b'open: No such file or directory')
            return real(['true'], *args, **kwargs)
        with mock.patch.object(candidate.subprocess, 'run', run), \
                mock.patch.object(candidate.shutil, 'which', return_value='/usr/bin/unshare'):
            self.assertFalse(candidate.can_be_root())
        self.assertIn('tun', candidate.NEEDS_SERVER)


if __name__ == '__main__':
    unittest.main()
