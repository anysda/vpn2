"""wg candidate check: whitespace is what wg(8) skips, C isspace(), not Python's wider \\s."""
from pathlib import Path
import tempfile
import unittest

from test_apply import HEADER, KEY, Host, apply, peer

# Python's \s and str.strip() take these as whitespace; wg reads them as part of
# the line and fails to parse it.
FOREIGN = ('\x1c', '\x1f', '\x85', '\xa0', ' ', '　')
# What wg skips anywhere in a line. The fake host splits syncconf input with
# splitlines(), so \v and \f go only through the check itself.
C_SPACE = (' ', '\t')
C_SPACE_CHECK_ONLY = ('\x0b', '\x0c')
PRIVATE = f'PrivateKey = {KEY["s"]}'


class WireguardWhitespaceTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        (self.work / 'state').mkdir()
        self.host = Host(self.work)
        self.applier = apply.Applier(self.work / 'root', self.work / 'state', self.host)
        self.applier.settle = 0
        self.path = self.work / 'root/etc/wireguard/wg0.conf'
        self.path.parent.mkdir(parents=True)
        self.new = self.work / 'new.conf'

    def synced(self):
        return [data for argv, data in self.host.calls if argv[:2] == ['/usr/bin/wg', 'syncconf']]

    def rejected(self, header, peers):
        working = HEADER + peers
        self.path.write_text(working)
        self.host.calls.clear()
        self.new.write_text(header)
        with self.assertRaises(apply.Failed) as caught:
            self.applier.install('wg', self.new)
        self.assertEqual(caught.exception.code, 2, str(caught.exception))
        self.assertIn('candidate rejected, working config untouched', str(caught.exception))
        self.assertEqual(self.synced(), [])
        self.assertEqual(self.path.read_text(), working)

    def applied(self, header, peers):
        self.path.write_text(HEADER + peers)
        self.host.calls.clear()
        self.new.write_text(header)
        self.applier.install('wg', self.new)
        self.assertEqual(len(self.synced()), 1)
        self.assertEqual(self.path.read_text(), header.rstrip(''.join(C_SPACE) + '\n') + '\n' + peers)

    def test_private_key_with_foreign_whitespace_is_rejected(self):
        for char in FOREIGN:
            for line in (PRIVATE + char, PRIVATE.replace(' =', char + '='), char + PRIVATE,
                         PRIVATE.replace('= ', '=' + char)):
                with self.subTest(line=ascii(line)):
                    self.rejected(HEADER.replace(PRIVATE, line), peer('a'))

    def test_private_key_with_foreign_whitespace_mid_header_is_rejected(self):
        for char in FOREIGN:
            with self.subTest(char=ascii(char)):
                self.rejected(f'[Interface]\n{PRIVATE}{char}\nAddress = 10.66.66.1/24\nListenPort = 51820\n',
                              peer('a'))

    def test_public_key_with_foreign_whitespace_is_rejected(self):
        public = f'PublicKey = {KEY["a"]}'
        for char in FOREIGN:
            for line in (public + char, public.replace(' =', char + '='), char + public):
                with self.subTest(line=ascii(line)):
                    self.rejected(HEADER, peer('a').replace(public, line))

    def test_allowed_ips_with_foreign_whitespace_is_rejected(self):
        allowed = 'AllowedIPs = 10.66.66.2/32'
        for char in FOREIGN:
            for line in (allowed + char, allowed.replace(' =', char + '='), char + allowed,
                         allowed + ',' + char + 'fd66:66::2/128'):
                with self.subTest(line=ascii(line)):
                    self.rejected(HEADER, peer('a').replace(allowed, line))

    def test_empty_allowed_ips_is_rejected(self):
        # wg: Line unrecognized: `AllowedIPs='
        for line in ('AllowedIPs =', 'AllowedIPs = ', 'AllowedIPs=\t'):
            with self.subTest(line=ascii(line)):
                self.rejected(HEADER, peer('a').replace('AllowedIPs = 10.66.66.2/32', line))

    def test_lines_wg_reads_are_applied(self):
        # wg drops C whitespace anywhere in the line, takes "off" in any case and a
        # leading minus that removes the address.
        for mark in ('Off', 'OFF'):
            with self.subTest(mark=mark):
                self.applied(HEADER + f'FwMark = {mark}\n', peer('a'))
        for ips in ('10.66.66.4 /32', '-10.66.66.4/32', '10.66.66.2/32, - fd66:66::2/128'):
            with self.subTest(ips=ips):
                self.applied(HEADER, peer('a').replace('10.66.66.2/32', ips, 1))

    def test_plain_config_is_applied(self):
        self.applied(HEADER, peer('a') + peer('b'))

    @staticmethod
    def spaced(char):
        header = HEADER.replace(PRIVATE, f'{char}PrivateKey{char}={char}{KEY["s"]}{char}').replace(
            'ListenPort = 51820', f'ListenPort{char}={char}51820{char}')
        peers = peer('a').replace(f'PublicKey = {KEY["a"]}', f'{char}PublicKey{char}={char}{KEY["a"]}{char}')
        return header, peers.replace('AllowedIPs = 10.66.66.2/32',
                                     f'AllowedIPs{char}={char}10.66.66.2/32,{char}fd66:66::2/128{char}')

    def test_c_whitespace_around_keys_and_values_is_applied(self):
        for char in C_SPACE:
            with self.subTest(char=ascii(char)):
                self.applied(*self.spaced(char))

    def test_vertical_tab_and_form_feed_pass_the_check(self):
        for char in C_SPACE_CHECK_ONLY:
            with self.subTest(char=ascii(char)):
                header, peers = self.spaced(char)
                self.applier.check('wg', header + peers, self.new)


if __name__ == '__main__':
    unittest.main()
