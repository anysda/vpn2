"""Candidate lines are split as openvpn and wg(8) split them: at a newline only."""
from pathlib import Path
import tempfile
import unittest

from test_apply import HEADER, KEY, Host, apply, peer
import test_apply_openvpn_candidate as candidate

WORKING = candidate.WORKING
# str.splitlines() also breaks a line at these; openvpn 2.6 and 2.7 do not.
# A carriage return is left out: reading the candidate turns it into a newline.
SEPARATORS = ('\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\x85', ' ', ' ')


@unittest.skipUnless(Path(candidate.OPENVPN).exists(), 'needs ' + candidate.OPENVPN)
class OpenvpnLineSplitTests(unittest.TestCase):
    setUp = candidate.OpenvpnCandidateTests.setUp
    rejected = candidate.OpenvpnCandidateTests.rejected
    applied = candidate.OpenvpnCandidateTests.applied

    def test_directive_glued_to_a_path_is_rejected_before_restart(self):
        # openvpn reads one line here: the ccd path is "ccd<separator>persist-key".
        for separator in SEPARATORS:
            with self.subTest(separator=hex(ord(separator))):
                self.rejected(WORKING.replace('client-config-dir ccd\n',
                                              f'client-config-dir ccd{separator}persist-key\n'),
                              'client-config-dir')

    def test_crlf_candidate_is_applied(self):
        self.new.write_bytes(WORKING.replace('\n', '\r\n').encode())
        self.applier.install('openvpn', self.new)
        self.assertEqual((self.base / 'server.conf').read_text(), WORKING)
        self.assertEqual(len(self.host.ran('restart')), 1)

    def test_working_candidate_is_applied(self):
        self.applied(WORKING + 'persist-key\npersist-tun\n')


class WireguardLineSplitTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.host = Host(self.root)
        self.applier = apply.Applier(self.root / 'root', self.root / 'state', self.host)
        self.applier.settle = 0
        (self.root / 'state').mkdir()
        self.path = self.root / 'root/etc/wireguard/wg0.conf'
        self.path.parent.mkdir(parents=True)
        self.new = self.root / 'new.conf'

    def loaded(self):
        """Every line wg syncconf gets is a whole line of the file wg-quick reads."""
        body = [data for argv, data in self.host.calls if argv[:2] == ['/usr/bin/wg', 'syncconf']][-1]
        lines = self.path.read_text().split('\n')
        return [line for line in body.split('\n') if line and line not in lines]

    def test_value_with_a_separator_is_not_a_key_in_the_interface(self):
        for separator in SEPARATORS:
            with self.subTest(separator=hex(ord(separator))):
                self.path.write_text(HEADER + peer('a'))
                self.new.write_text(HEADER.replace('Address = 10.66.66.1/24\n',
                                                   f'Address = 10.66.66.1/24{separator}FwMark = 51820\n'))
                self.applier.install('wg', self.new)
                self.assertEqual(self.loaded(), [])

    def test_value_with_a_separator_is_not_a_key_in_a_peer(self):
        for separator in SEPARATORS:
            with self.subTest(separator=hex(ord(separator))):
                self.path.write_text(HEADER + peer('a').replace(
                    'AllowedIPs = 10.66.66.2/32\n', f'AllowedIPs = 10.66.66.2/32{separator}AllowedIPs = 0.0.0.0/0\n'))
                self.new.write_text(HEADER)
                self.applier.install('wg', self.new)
                self.assertEqual(self.loaded(), [])

    def test_plain_config_is_loaded_line_by_line(self):
        self.path.write_text(HEADER + peer('a'))
        self.new.write_text(HEADER)
        self.applier.install('wg', self.new)
        self.assertEqual(self.loaded(), [])
        body = [data for argv, data in self.host.calls if argv[:2] == ['/usr/bin/wg', 'syncconf']][-1]
        self.assertIn(f'PublicKey = {KEY["a"]}', body.split('\n'))
        self.assertIn(f'PrivateKey = {KEY["s"]}', body.split('\n'))


if __name__ == '__main__':
    unittest.main()
