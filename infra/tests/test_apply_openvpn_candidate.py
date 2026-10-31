"""OpenVPN candidate check: log directives and cipher names, with the real openvpn."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_apply import Host, apply

OPENVPN = '/usr/sbin/openvpn'
WORKING = ('port 1194\nproto udp\ndev tun\nserver 10.8.0.0 255.255.255.0\nca pki/ca.crt\n'
           'cert pki/server.crt\nkey pki/server.key\ndh none\ntls-crypt pki/tls-crypt.key\n'
           'crl-verify pki/crl.pem\nclient-config-dir ccd\n')
# The cipher check starts a TLS server in a network namespace of its own, as root.
AS_ROOT = [] if os.geteuid() == 0 else ['sudo', '-n']
NEEDS_SERVER = 'needs root or sudo -n, unshare --net and a tun device'


def can_be_root():
    # Root without CAP_SYS_ADMIN (a container) runs true, not unshare --net; a container
    # without /dev/net/tun or CAP_NET_ADMIN runs unshare, but the server opens no tun there.
    return bool(shutil.which('unshare')) and all(subprocess.run(
        AS_ROOT + ['unshare', '--net'] + probe, capture_output=True).returncode == 0
        for probe in (['true'], ['ip', 'tuntap', 'add', 'dev', 'vpn2check', 'mode', 'tun']))


SERVER_RUNS = can_be_root()
needs_server = unittest.skipUnless(SERVER_RUNS, NEEDS_SERVER)


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnCandidateTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.root = self.work / 'root'
        self.state = self.work / 'state'
        self.state.mkdir()
        self.new = self.work / 'new.conf'
        self.host = Host(self.root)
        self.applier = apply.Applier(self.root, self.state, self.host)
        self.applier.settle = 0
        # The fake host looks at sing-box for every unit it shows.
        (self.root / 'etc/sing-box').mkdir(parents=True)
        (self.root / 'etc/sing-box/config.json').write_text('{"inbounds": []}')
        self.base = self.root / 'etc/openvpn/server'
        (self.base / 'pki').mkdir(parents=True)
        (self.base / 'ccd').mkdir()

        def tool(*argv):
            subprocess.run(argv, cwd=self.base / 'pki', check=True, capture_output=True)
        for name in ('ca', 'server'):
            tool('openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                 '-nodes', '-keyout', name + '.key', '-out', name + '.crt', '-subj', '/CN=' + name, '-days', '2')
        tool('openvpn', '--genkey', 'secret', 'tls-crypt.key')
        (self.base / 'pki/index.txt').write_text('')
        (self.base / 'pki/crlnumber').write_text('01\n')
        (self.base / 'pki/openssl.cnf').write_text('[ca]\ndefault_ca = x\n[x]\ndatabase = index.txt\n'
                                                   'crlnumber = crlnumber\ndefault_md = sha256\n'
                                                   'default_crl_days = 2\n')
        tool('openssl', 'ca', '-batch', '-config', 'openssl.cnf', '-keyfile', 'ca.key', '-cert', 'ca.crt',
             '-gencrl', '-out', 'crl.pem')
        self.new.write_text(WORKING)
        self.applier.install('openvpn', self.new)
        self.host.calls.clear()

    def rejected(self, body, word):
        self.new.write_text(body)
        with self.assertRaises(apply.Failed) as caught:
            self.applier.install('openvpn', self.new)
        failure = caught.exception
        self.assertEqual(failure.code, 2)
        self.assertIn('candidate rejected, working config untouched', str(failure))
        self.assertIn(word, str(failure))
        self.assertEqual(self.host.ran('restart'), [])
        self.assertEqual((self.base / 'server.conf').read_text(), WORKING)
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ['ccd', 'pki', 'server.conf'])

    def applied(self, body):
        self.new.write_text(body)
        self.applier.install('openvpn', self.new)
        self.assertEqual((self.base / 'server.conf').read_text(), body)
        self.assertEqual(len(self.host.ran('restart')), 1)

    def log_is_rejected(self, directive):
        log = self.work / 'keep.log'
        log.write_text('earlier lines\n')
        inode = log.stat().st_ino
        try:
            self.rejected(WORKING + f'{directive} {log}\n', directive)
        finally:
            # The symptom itself: the check must not touch the file the candidate names.
            self.assertEqual(log.read_text(), 'earlier lines\n')
            self.assertEqual(log.stat().st_ino, inode)

    def test_log_is_rejected_and_its_file_untouched(self):
        self.log_is_rejected('log')

    def test_log_append_is_rejected_and_its_file_untouched(self):
        self.log_is_rejected('log-append')

    @needs_server
    def test_unknown_cipher_is_rejected_before_restart(self):
        self.rejected(WORKING + 'cipher NOPE\n', 'NOPE')

    def test_unknown_data_cipher_is_rejected_before_restart(self):
        self.rejected(WORKING + 'data-ciphers AES-256-GCM:NOPE\n', 'NOPE')

    @needs_server
    def test_known_ciphers_are_applied(self):
        listed = subprocess.run([OPENVPN, '--show-ciphers'], capture_output=True, text=True).stdout
        names = {line.split()[0] for line in listed.splitlines() if line.strip()}
        self.assertLessEqual({'AES-256-GCM', 'CHACHA20-POLY1305'}, names)
        self.applied(WORKING + 'cipher AES-256-GCM\ndata-ciphers AES-256-GCM:CHACHA20-POLY1305\n')

    def test_working_candidate_is_applied_as_before(self):
        self.applied(WORKING + 'keepalive 10 60\n')


if __name__ == '__main__':
    unittest.main()
