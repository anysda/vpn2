"""OpenVPN candidate check: auth and the cipher names openvpn 2.7 accepts, with the real openvpn."""
from pathlib import Path
import unittest

from test_apply import apply
import test_apply_openvpn_candidate as candidate

WORKING = candidate.WORKING

# openvpn 2.7 starts a server with each of these (cipher, data-ciphers and auth
# go through OpenSSL names, any case), while --show-ciphers prints no alias.
SYNONYMS = ('AES256', 'id-aes256-GCM', 'aes-256-gcm')
DIGESTS = ('sha256', 'SHA2-256')


@unittest.skipUnless(Path(candidate.OPENVPN).exists(), 'needs ' + candidate.OPENVPN)
class OpenvpnAuthTests(unittest.TestCase):
    # The temporary PKI and helpers of the cipher tests, not their tests.
    setUp = candidate.OpenvpnCandidateTests.setUp
    rejected = candidate.OpenvpnCandidateTests.rejected
    applied = candidate.OpenvpnCandidateTests.applied

    def restored(self, good):
        (self.base / 'server.conf').write_text(good)
        self.applier.mark('openvpn')
        # The fake host keeps a unit with BROKEN in its config down after the restart.
        self.new.write_text(WORKING + '# BROKEN\n')
        with self.assertRaises(apply.Failed) as caught:
            self.applier.install('openvpn', self.new)
        self.assertEqual(caught.exception.code, 3, str(caught.exception))
        self.assertIn('previous config restored', str(caught.exception))
        self.assertEqual((self.base / 'server.conf').read_text(), good)

    def test_unknown_auth_is_rejected_before_restart(self):
        self.rejected(WORKING + 'auth NOPE\n', 'NOPE')

    @candidate.needs_server
    def test_cipher_synonyms_openvpn_accepts_are_applied(self):
        for name in SYNONYMS:
            with self.subTest(name=name):
                self.applied(WORKING + f'cipher {name}\n')
                self.host.calls.clear()

    def test_data_cipher_synonyms_openvpn_accepts_are_applied(self):
        for name in SYNONYMS:
            with self.subTest(name=name):
                self.applied(WORKING + f'data-ciphers {name}:CHACHA20-POLY1305\n')
                self.host.calls.clear()

    def test_auth_digest_synonyms_openvpn_accepts_are_applied(self):
        for name in DIGESTS:
            with self.subTest(name=name):
                self.applied(WORKING + f'auth {name}\n')
                self.host.calls.clear()

    @candidate.needs_server
    def test_last_good_with_a_cipher_synonym_is_restored(self):
        self.restored(WORKING + 'cipher AES256\n')

    def test_last_good_with_a_data_cipher_synonym_is_restored(self):
        self.restored(WORKING + 'data-ciphers id-aes256-GCM:AES-256-GCM\n')

    @candidate.needs_server
    def test_ciphers_and_auth_of_29_openvpn_are_applied(self):
        self.applied(WORKING + 'auth SHA256\ncipher AES-256-GCM\ndata-ciphers AES-256-GCM:CHACHA20-POLY1305\n')

    def test_tls_cipher_is_rejected_before_restart(self):
        self.rejected(WORKING + 'tls-cipher NOPE\n', 'tls-cipher')

    @candidate.needs_server
    def test_auth_none_with_an_aead_cipher_is_applied(self):
        # openvpn 2.7 starts a server with it: an AEAD cipher carries its own tag.
        self.applied(WORKING + 'auth none\ncipher AES-256-GCM\ndata-ciphers AES-256-GCM:CHACHA20-POLY1305\n')


if __name__ == '__main__':
    unittest.main()
