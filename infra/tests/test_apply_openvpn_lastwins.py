"""OpenVPN candidate check: a repeated cipher, data-ciphers or auth counts by its last line, with the real openvpn."""
from pathlib import Path
import unittest

import test_apply_openvpn_auth as auth
import test_apply_openvpn_candidate as candidate
import test_apply_openvpn_datalist as datalist

WORKING = candidate.WORKING

# openvpn 2.6 and 2.7 start a server with the last of each, whatever came above it.
GOOD = {'cipher': 'AES-256-GCM', 'data-ciphers': 'AES-256-GCM', 'auth': 'SHA256'}


@unittest.skipUnless(Path(candidate.OPENVPN).exists(), 'needs ' + candidate.OPENVPN)
class OpenvpnLastWinsTests(unittest.TestCase):
    setUp = candidate.OpenvpnCandidateTests.setUp
    rejected = candidate.OpenvpnCandidateTests.rejected
    applied = candidate.OpenvpnCandidateTests.applied
    restored = auth.OpenvpnAuthTests.restored

    def test_bad_above_good_last_is_applied(self):
        for name, good in GOOD.items():
            with self.subTest(name=name):
                self.applied(WORKING + f'{name} NOPE\n{name} {good}\n')
                self.host.calls.clear()

    def test_good_above_bad_last_is_rejected_before_restart(self):
        for name, good in GOOD.items():
            with self.subTest(name=name):
                self.rejected(WORKING + f'{name} {good}\n{name} NOPE\n', 'NOPE')

    def test_short_list_last_after_a_too_long_one_is_applied(self):
        self.applied(WORKING + f'data-ciphers {datalist.TOO_LONG}\ndata-ciphers AES-256-GCM\n')

    def test_too_long_list_last_is_rejected_before_restart(self):
        self.rejected(WORKING + f'data-ciphers AES-256-GCM\ndata-ciphers {datalist.TOO_LONG}\n', 'data-ciphers')

    def test_last_good_with_a_repeated_auth_is_restored(self):
        self.restored(WORKING + 'auth NOPE\nauth SHA256\n')


if __name__ == '__main__':
    unittest.main()
