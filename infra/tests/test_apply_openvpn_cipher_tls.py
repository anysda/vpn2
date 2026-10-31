"""OpenVPN candidate check: a cipher is taken as a TLS server takes it, against a real server start."""
from pathlib import Path
import subprocess
import threading
import unittest

from test_apply import apply
import test_apply_openvpn_candidate as candidate

OPENVPN = candidate.OPENVPN
WORKING = candidate.WORKING
AS_ROOT = candidate.AS_ROOT
# openvpn 2.6 and 2.7 decide each of these differently in --test-crypto and in a TLS
# server start: BF-CBC is the fallback for old clients there, case and "?" included.
NAMES = ('BF-CBC', 'bf-cbc', 'NOPE', 'AES256', 'AES-256-GCM', 'AES-128-CBC', '', '?BF-CBC')
LISTS = (('data-ciphers', 'BF-CBC'), ('data-ciphers', 'AES-256-GCM:BF-CBC'),
         ('data-ciphers-fallback', 'BF-CBC'))
can_be_root = candidate.can_be_root


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
@candidate.needs_server
class OpenvpnCipherTlsTests(unittest.TestCase):
    # The temporary PKI and helpers of the cipher tests, not their tests.
    setUp = candidate.OpenvpnCandidateTests.setUp
    rejected = candidate.OpenvpnCandidateTests.rejected
    applied = candidate.OpenvpnCandidateTests.applied

    def server_starts(self, body):
        """The oracle: openvpn starts a TLS server from the config, or exits on it."""
        conf = self.work / 'oracle.conf'
        conf.write_text(body)
        server = subprocess.Popen(AS_ROOT + ['unshare', '--net', OPENVPN, '--cd', str(self.base),
                                             '--config', str(conf), '--verb', '3'],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        timer = threading.Timer(60, server.kill)
        timer.start()
        said = []
        try:
            for line in server.stdout:
                said.append(line.rstrip())
                if 'Initialization Sequence Completed' in line:
                    return True, said[-1]
            return False, said[-1] if said else f'rc={server.wait()}'
        finally:
            timer.cancel()
            if server.poll() is None:
                server.terminate()
            server.wait()
            server.stdout.close()

    def decided_as_server(self, body, option):
        starts, said = self.server_starts(body)
        with self.subTest(line=body.removeprefix(WORKING), server_starts=starts, server_said=said):
            if starts:
                try:
                    self.applied(body)
                except apply.Failed as error:
                    self.fail(f'the server starts, the check said: {error}')
                self.host.calls.clear()
                self.applied(WORKING)
            else:
                self.rejected(body, option)
        self.host.calls.clear()

    def test_the_oracle_starts_the_working_config(self):
        starts, said = self.server_starts(WORKING)
        self.assertTrue(starts, said)
        starts, said = self.server_starts(WORKING + 'cipher NOPE\n')
        self.assertFalse(starts, said)

    def test_cipher_is_decided_as_a_tls_server_starts(self):
        for name in NAMES:
            self.decided_as_server(WORKING + f'cipher {name}\n'.replace(' \n', '\n'), 'cipher')

    def test_cipher_lists_are_decided_as_a_tls_server_starts(self):
        for option, names in LISTS:
            self.decided_as_server(WORKING + f'{option} {names}\n', option)

    def test_last_good_with_cipher_bf_cbc_is_restored_as_the_server_starts(self):
        good = WORKING + 'cipher BF-CBC\n'
        starts, said = self.server_starts(good)
        (self.base / 'server.conf').write_text(good)
        self.applier.mark('openvpn')
        # The fake host keeps a unit with BROKEN in its config down after the restart.
        self.new.write_text(WORKING + '# BROKEN\n')
        with self.assertRaises(apply.Failed) as caught:
            self.applier.install('openvpn', self.new)
        self.assertEqual(caught.exception.code, 3 if starts else 4, f'{caught.exception}; server: {said}')
        if starts:
            self.assertEqual((self.base / 'server.conf').read_text(), good)


if __name__ == '__main__':
    unittest.main()
