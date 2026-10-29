"""OpenVPN candidate lines are split into words as openvpn 2.7 splits them, with the real openvpn."""
import shutil
import unittest

import test_apply_openvpn_candidate as candidate
from test_apply_openvpn_candidate import WORKING


class OpenvpnTokenizeTests(unittest.TestCase):
    setUp = candidate.OpenvpnCandidateTests.setUp
    rejected = candidate.OpenvpnCandidateTests.rejected
    applied = candidate.OpenvpnCandidateTests.applied

    def file_is_untouched(self, line_for, word):
        log = self.work / 'keep.log'
        log.write_text('earlier lines\n')
        inode = log.stat().st_ino
        try:
            self.rejected(WORKING + line_for(log) + '\n', word)
        finally:
            self.assertEqual(log.read_text(), 'earlier lines\n')
            self.assertEqual(log.stat().st_ino, inode)

    # openvpn opens a quote only at the start of a word and reads ; as a comment.
    def test_semicolon_comment_with_apostrophe_is_applied(self):
        self.applied(WORKING + "; don't touch\n")

    def test_apostrophe_inside_a_word_is_applied(self):
        self.applied(WORKING + "setenv NAME it's\n")

    def test_hash_inside_a_word_is_applied(self):
        self.applied(WORKING + 'setenv NAME a#b\n')

    def test_quoted_log_is_rejected(self):
        self.file_is_untouched(lambda log: f'"log" "{log}"', 'log')

    def test_dashed_log_append_is_rejected(self):
        self.file_is_untouched(lambda log: f'--log-append {log}', 'log-append')

    def test_backslash_spelled_log_is_rejected(self):
        self.file_is_untouched(lambda log: f'lo\\g {log}', 'rejected')

    def test_cd_with_relative_crl_is_rejected_before_restart(self):
        other = self.work / 'other'
        shutil.copytree(self.base / 'pki', other / 'pki')
        (other / 'ccd').mkdir()
        self.rejected(WORKING.replace('crl-verify', f'cd {other}\ncrl-verify'), 'cd')

    # openvpn applies the last of repeated crl-verify and client-config-dir lines.
    def test_repeated_crl_verify_with_other_path_is_rejected_before_restart(self):
        other = self.work / 'other.pem'
        shutil.copy(self.base / 'pki/crl.pem', other)
        self.rejected(WORKING + f'crl-verify {other}\n', 'crl-verify')

    def test_repeated_client_config_dir_with_other_path_is_rejected_before_restart(self):
        other = self.work / 'other-ccd'
        other.mkdir()
        self.rejected(WORKING + f'client-config-dir {other}\n', 'client-config-dir')

    def test_repeated_crl_verify_with_managed_path_is_rejected_before_restart(self):
        self.rejected(WORKING + 'crl-verify pki/crl.pem\n', 'crl-verify')

    def test_inline_crl_verify_after_managed_is_rejected_before_restart(self):
        crl = (self.base / 'pki/crl.pem').read_text()
        self.rejected(WORKING + f'<crl-verify>\n{crl}</crl-verify>\n', 'crl-verify')

    def test_server_template_is_applied(self):
        pki, base = self.base / 'pki', self.base
        self.applied(f'port 1194\nproto udp\ndev tun0\ntopology subnet\n'
                     f'server 10.8.0.0 255.255.255.0 nopool\n'
                     f'ifconfig-pool 10.8.0.240 10.8.0.253 255.255.255.0\n'
                     f'ca   {pki}/ca.crt\ncert {pki}/server.crt\nkey  {pki}/server.key\n'
                     f'dh none\ntls-crypt {pki}/tls-crypt.key\n'
                     f'crl-verify {pki}/crl.pem\nclient-config-dir {base}/ccd\n'
                     f'keepalive 5 30\ncipher AES-256-GCM\n'
                     f'data-ciphers AES-256-GCM:CHACHA20-POLY1305\nauth SHA256\n'
                     f'persist-key\npersist-tun\nsndbuf 0\nrcvbuf 0\nfast-io\n')

    def test_unclosed_quote_is_rejected_with_code_2(self):
        self.rejected(WORKING + 'setenv NAME "unclosed\n', 'quotation')

    def test_unclosed_single_quote_is_rejected_with_code_2(self):
        self.rejected(WORKING + "setenv NAME 'unclosed\n", 'quotation')


if __name__ == '__main__':
    unittest.main()
