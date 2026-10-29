"""OpenVPN candidate check: data-ciphers as a whole list, with the real openvpn."""
from pathlib import Path
import unittest

from test_apply import apply
import test_apply_openvpn_candidate as candidate

WORKING = candidate.WORKING

# Each name is known, the list is not: openvpn 2.6 and 2.7 take a data-ciphers
# list of up to 124 characters and fail the start from 125 on.
REVIEW = ('AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-256-CBC:AES-128-CBC:AES-192-GCM:'
          'AES-192-CBC:ARIA-256-CBC:CAMELLIA-256-CBC:SM4-CBC')
LONGEST = ('CHACHA20-POLY1305:CAMELLIA-128-CBC:AES-256-GCM:AES-128-GCM:AES-256-CBC:AES-192-GCM:'
           'ARIA-256-CBC:AES-192-CBC:CAMELLIA-192-CBC')
TOO_LONG = ('AES-192-GCM:CAMELLIA-128-CBC:AES-256-CBC:CHACHA20-POLY1305:AES-128-GCM:ARIA-128-CBC:'
            'AES-256-GCM:CAMELLIA-192-CBC:ARIA-192-CBC')


@unittest.skipUnless(Path(candidate.OPENVPN).exists(), 'needs ' + candidate.OPENVPN)
class OpenvpnDataCipherListTests(unittest.TestCase):
    setUp = candidate.OpenvpnCandidateTests.setUp
    rejected = candidate.OpenvpnCandidateTests.rejected
    applied = candidate.OpenvpnCandidateTests.applied

    def test_lengths(self):
        self.assertEqual((len(REVIEW), len(LONGEST), len(TOO_LONG)), (127, 124, 125))

    def test_list_openvpn_rejects_is_rejected_before_restart(self):
        for names in (REVIEW, TOO_LONG):
            with self.subTest(length=len(names)):
                self.rejected(WORKING + f'data-ciphers {names}\n', 'data-ciphers')

    def test_list_up_to_the_limit_is_applied(self):
        self.applied(WORKING + f'data-ciphers {LONGEST}\n')

    def test_unknown_optional_cipher_is_skipped(self):
        # openvpn drops ?NAME before it measures the list.
        for names in ('?NOPE:AES-256-GCM', '?NOPE:' + LONGEST):
            with self.subTest(names=names):
                self.applied(WORKING + f'data-ciphers {names}\n')
                self.host.calls.clear()

    def test_only_unknown_optional_cipher_is_rejected(self):
        self.rejected(WORKING + 'data-ciphers ?NOPE\n', 'no known cipher')

    def test_last_good_with_a_list_openvpn_rejects_is_not_restored(self):
        good = WORKING + f'data-ciphers {REVIEW}\n'
        (self.base / 'server.conf').write_text(good)
        self.applier.mark('openvpn')
        # The fake host keeps a unit with BROKEN in its config down after the restart.
        self.new.write_text(WORKING + '# BROKEN\n')
        with self.assertRaises(apply.Failed) as caught:
            self.applier.install('openvpn', self.new)
        self.assertEqual(caught.exception.code, 4, str(caught.exception))
        self.assertIn('restore failed: data-ciphers', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
