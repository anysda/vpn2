"""OpenVPN candidate lines are split into words as openvpn 2.7 splits them, with the real openvpn."""
import re
import shutil
import subprocess
import unittest

from test_apply import ROOT
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
        self.applied(WORKING + "push it's\n")

    def test_hash_inside_a_word_is_applied(self):
        self.applied(WORKING + 'push a#b\n')

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
        self.rejected(WORKING + 'push "unclosed\n', 'quotation')

    def test_unclosed_single_quote_is_rejected_with_code_2(self):
        self.rejected(WORKING + "push 'unclosed\n", 'quotation')

    # openvpn runs "setenv opt X" as the directive X itself.
    def test_setenv_opt_log_is_rejected(self):
        self.file_is_untouched(lambda log: f'setenv opt log {log}', 'setenv')

    def test_setenv_opt_log_append_is_rejected(self):
        self.file_is_untouched(lambda log: f'setenv opt log-append {log}', 'setenv')

    def test_setenv_opt_config_is_rejected(self):
        def line_for(log):
            (self.work / 'inc.conf').write_text(f'log {log}\n')
            return f'setenv opt config {self.work / "inc.conf"}'
        self.file_is_untouched(line_for, 'setenv')

    def test_setenv_opt_cd_is_rejected(self):
        other = self.work / 'other'
        shutil.copytree(self.base / 'pki', other / 'pki')
        (other / 'ccd').mkdir()
        self.rejected(WORKING.replace('crl-verify', f'setenv opt cd {other}\ncrl-verify'), 'setenv')

    def test_setenv_opt_crl_verify_is_rejected(self):
        other = self.work / 'other.pem'
        shutil.copy(self.base / 'pki/crl.pem', other)
        self.rejected(WORKING + f'setenv opt crl-verify {other}\n', 'setenv')

    # openvpn drops a UTF-8 BOM at the start of the file.
    def test_bom_before_log_is_rejected(self):
        log = self.work / 'keep.log'
        log.write_text('earlier lines\n')
        try:
            self.rejected(f'\ufeff log {log}\n' + WORKING, 'log')
        finally:
            self.assertEqual(log.read_text(), 'earlier lines\n')

    def test_bom_before_cd_is_rejected(self):
        other = self.work / 'other'
        shutil.copytree(self.base / 'pki', other / 'pki')
        (other / 'ccd').mkdir()
        self.rejected(f'\ufeff cd {other}\n' + WORKING, 'cd')

    def test_providers_is_rejected_before_openvpn_runs(self):
        self.rejected(WORKING + f'providers {self.work / "evil.so"}\n', 'providers')
        self.assertEqual(self.host.ran('/usr/sbin/openvpn'), [])

    def test_directive_the_template_does_not_write_is_rejected(self):
        self.rejected(WORKING + 'tun-mtu 1400\n', 'tun-mtu')
        self.assertEqual(self.host.ran('/usr/sbin/openvpn'), [])

    def template(self, proto, fastio):
        script = (ROOT / 'infra/scripts/29-openvpn.sh').read_text()
        body = re.search(r'cat > "\$OVPN_DIR/server.conf" <<EOF\n(.*?\nEOF\n)', script, re.S)[1]
        env = {'OVPN_PORT': '1194', 'OVPN_SERVER_PROTO': proto, 'OVPN_SUBNET': '10.8.0.0',
               'PKI': str(self.base / 'pki'), 'OVPN_DIR': str(self.base), 'OVPN_FASTIO': fastio,
               'OVPN_CONTROL_USER': 'root', 'PATH': '/usr/bin:/bin'}
        text = subprocess.run(['bash', '-c', 'cat <<EOF\n' + body], env=env, check=True,
                              capture_output=True, text=True).stdout
        self.assertNotIn('$', text)
        return text + 'route 10.8.1.0 255.255.255.0\n'

    # The allowed directives are exactly what 29-openvpn.sh writes.
    def test_udp_server_template_from_the_script_is_applied(self):
        self.applied(self.template('udp', 'fast-io'))

    def test_tcp_server_template_from_the_script_is_applied(self):
        self.applied(self.template('tcp-server', ''))


if __name__ == '__main__':
    unittest.main()
