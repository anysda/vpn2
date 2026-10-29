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

    def test_unclosed_quote_is_rejected_with_code_2(self):
        self.rejected(WORKING + 'setenv NAME "unclosed\n', 'quotation')

    def test_unclosed_single_quote_is_rejected_with_code_2(self):
        self.rejected(WORKING + "setenv NAME 'unclosed\n", 'quotation')


if __name__ == '__main__':
    unittest.main()
