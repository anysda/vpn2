"""OpenVPN candidate check: "?NAME" only in data-ciphers, and auth digests openvpn starts with, with the real openvpn."""
from pathlib import Path
import unittest

import test_apply_openvpn_auth as auth
import test_apply_openvpn_candidate as candidate

WORKING = candidate.WORKING

# openvpn 2.6 and 2.7 start a server with these digests, which --test-crypto refuses.
XOF_DIGESTS = ('SHAKE128', 'SHAKE256')


@unittest.skipUnless(Path(candidate.OPENVPN).exists(), 'needs ' + candidate.OPENVPN)
class OpenvpnQmarkTests(unittest.TestCase):
    setUp = candidate.OpenvpnCandidateTests.setUp
    rejected = candidate.OpenvpnCandidateTests.rejected
    applied = candidate.OpenvpnCandidateTests.applied
    restored = auth.OpenvpnAuthTests.restored

    def test_qmark_outside_data_ciphers_is_rejected_before_restart(self):
        for line in ('cipher ?AES-256-GCM', 'auth ?SHA256'):
            with self.subTest(line=line):
                if line.startswith('cipher') and not candidate.SERVER_RUNS:
                    self.skipTest(candidate.NEEDS_SERVER)
                self.rejected(WORKING + line + '\n', line.split()[1])

    @candidate.needs_server
    def test_qmark_cipher_last_after_a_good_one_is_rejected_before_restart(self):
        self.rejected(WORKING + 'cipher AES-256-GCM\ncipher ?AES-256-GCM\n', '?AES-256-GCM')

    def test_qmark_in_data_ciphers_is_applied(self):
        self.applied(WORKING + 'data-ciphers ?NOPE:AES-256-GCM\n')

    def test_xof_digests_are_applied(self):
        for name in XOF_DIGESTS:
            with self.subTest(name=name):
                self.applied(WORKING + f'auth {name}\n')
                self.host.calls.clear()

    def test_last_good_with_a_xof_digest_is_restored(self):
        self.restored(WORKING + 'auth SHAKE256\n')

    def test_digests_openvpn_refuses_are_rejected_before_restart(self):
        for name in ('whirlpool', 'NOPE'):
            with self.subTest(name=name):
                self.rejected(WORKING + f'auth {name}\n', name)


if __name__ == '__main__':
    unittest.main()
