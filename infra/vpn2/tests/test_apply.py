"""Candidate apply: rejected candidates, interrupted stages, rollback and access."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[3]))
# apply.py берёт разбор wg0.conf из пакета.
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))
spec = importlib.util.spec_from_file_location('apply', ROOT / 'infra/vpn2/src/vpn2/control/apply.py')
apply = importlib.util.module_from_spec(spec)
sys.modules['apply'] = apply
spec.loader.exec_module(apply)

KEY = {name: name * 42 + 'A=' for name in 'abcs'}
HEADER = f'[Interface]\nAddress = 10.66.66.1/24\nListenPort = 51820\nPrivateKey = {KEY["s"]}\n'


def peer(name):
    return f'\n[Peer]\nPublicKey = {KEY[name]}\nAllowedIPs = 10.66.66.2/32\n'


class Interrupted(BaseException):
    pass


class Host:
    """Commands of a fake host. A service runs only a config without BROKEN."""

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.interrupt = None
        self.nft_fails = False
        self.shown = 0

    def __call__(self, argv, data=None):
        self.calls.append((argv, data))
        if self.interrupt and self.interrupt in ' '.join(argv):
            self.interrupt = None
            raise Interrupted()
        if argv[:2] == ['/usr/bin/wg', 'syncconf']:
            # As wg(8): a key outside its section is a parsing error.
            section = None
            for line in data.splitlines():
                if line.strip() in ('[Interface]', '[Peer]'):
                    section = line.strip()
                elif line.strip() and (section is None or line.split('=')[0].strip() not in {
                        '[Interface]': {'PrivateKey', 'ListenPort', 'FwMark'},
                        '[Peer]': {'PublicKey', 'PresharedKey', 'AllowedIPs'}}[section]):
                    raise RuntimeError('wg syncconf: Configuration parsing error')
        if argv[:2] == ['/usr/local/bin/sing-box', 'check']:
            try:
                json.loads(Path(argv[-1]).read_text())
            except ValueError as error:
                raise RuntimeError(f'sing-box check: {error}') from None
        if argv[0] == '/usr/sbin/openvpn':
            return apply.run(argv)
        if argv[:2] == ['systemctl', 'is-active']:
            config = {'sing-box': 'etc/sing-box/config.json',
                      'openvpn-server@server': 'etc/openvpn/server/server.conf'}[argv[-1]]
            if 'BROKEN' in (self.root / config).read_text():
                raise RuntimeError('systemctl is-active: inactive')
        if argv[:2] == ['systemctl', 'show']:
            # A unit whose config has CRASHLOOP is restarted by systemd each look.
            self.shown += 1
            looping = 'CRASHLOOP' in (self.root / 'etc/sing-box/config.json').read_text()
            return f'MainPID={100 + self.shown if looping else 100}\nNRestarts=0\n'
        if argv[:2] == ['/usr/sbin/nft', '-c'] and 'SYNTAX' in Path(argv[-1]).read_text():
            raise RuntimeError('nft -c: Error: syntax error, unexpected \'}\'')
        if argv[0] == '/usr/sbin/nft' and argv[1] == '-f' and self.nft_fails and 'restore' not in argv[2]:
            raise RuntimeError('nft -f: syntax error')
        if argv[:3] == ['/usr/sbin/nft', 'list', 'table']:
            return 'table inet anysda_ytquic {\n\tchain c {\n\t}\n}\n'
        return ''

    def ran(self, *words):
        return [argv for argv, _data in self.calls if all(word in argv for word in words)]


class ApplyTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name) / 'root'
        self.state = Path(work.name) / 'state'
        self.state.mkdir()
        self.host = Host(self.root)
        self.applier = self.fresh()
        self.config = self.root / 'etc/sing-box/config.json'
        self.config.parent.mkdir(parents=True)
        self.config.write_text('{"inbounds": [], "v": 1}')
        self.new = Path(work.name) / 'new.json'

    def fresh(self):
        applier = apply.Applier(self.root, self.state, self.host)
        applier.settle = 0
        return applier

    def install(self, resource, body):
        self.new.write_text(body)
        with self.assertRaises(apply.Failed) as caught:
            self.applier.install(resource, self.new)
        return caught.exception

    def test_broken_candidate_does_not_replace_working(self):
        failure = self.install('sing-box', '{"inbounds": [')
        self.assertEqual(failure.code, 2)
        self.assertIn('candidate rejected, working config untouched', str(failure))
        self.assertEqual(self.config.read_text(), '{"inbounds": [], "v": 1}')
        self.assertEqual(self.host.ran('restart'), [])
        self.assertEqual([path.name for path in self.config.parent.iterdir()], ['config.json'])

    def test_project_check_rejects_what_syntax_check_accepts(self):
        failure = self.install('sing-box', '{"inbounds": [{"listen_port": 1}, {"listen_port": 1}]}')
        self.assertEqual(failure.code, 2)
        self.assertIn('two inbounds on one port', str(failure))
        self.assertEqual(self.config.read_text(), '{"inbounds": [], "v": 1}')

    def test_same_port_on_v4_and_v6_loopback_is_accepted(self):
        body = ('{"inbounds": [{"listen": "127.0.0.1", "listen_port": 7899},'
                ' {"listen": "::1", "listen_port": 7899}]}')
        self.new.write_text(body)
        self.applier.install('sing-box', self.new)
        self.assertEqual(self.config.read_text(), body)

    def test_candidate_failing_after_apply_restores_last_good(self):
        failure = self.install('sing-box', '{"inbounds": [], "BROKEN": 1}')
        self.assertEqual(failure.code, 3)
        self.assertIn('previous config restored', str(failure))
        self.assertEqual(self.config.read_text(), '{"inbounds": [], "v": 1}')
        self.assertEqual(self.applier.good('sing-box').read_text(), '{"inbounds": [], "v": 1}')
        self.assertEqual(len(self.host.ran('restart', 'sing-box')), 2)
        self.assertFalse(self.applier.journal('sing-box').exists())

    def test_service_that_falls_after_start_restores_last_good(self):
        failure = self.install('sing-box', '{"inbounds": [], "CRASHLOOP": 1}')
        self.assertEqual(failure.code, 3)
        self.assertEqual(self.config.read_text(), '{"inbounds": [], "v": 1}')
        self.assertEqual(self.applier.good('sing-box').read_text(), '{"inbounds": [], "v": 1}')

    def test_last_good_is_marked_only_after_verification(self):
        self.new.write_text('{"inbounds": [], "v": 2}')
        self.applier.install('sing-box', self.new)
        self.assertEqual(self.applier.good('sing-box').read_text(), '{"inbounds": [], "v": 2}')
        self.host.interrupt = 'is-active'
        self.new.write_text('{"inbounds": [], "v": 3}')
        with self.assertRaises(Interrupted):
            self.applier.install('sing-box', self.new)
        self.assertEqual(self.applier.good('sing-box').read_text(), '{"inbounds": [], "v": 2}')

    def test_interrupted_after_rename_is_finished_by_next_run(self):
        self.applier.mark('sing-box')
        self.host.interrupt = 'restart'
        self.new.write_text('{"inbounds": [], "v": 2}')
        with self.assertRaises(Interrupted):
            self.applier.install('sing-box', self.new)
        self.assertEqual(json.loads(self.applier.journal('sing-box').read_text())['stage'], 'applied')
        self.fresh().resume('sing-box')
        self.assertEqual(self.config.read_text(), '{"inbounds": [], "v": 2}')
        self.assertEqual(self.applier.good('sing-box').read_text(), '{"inbounds": [], "v": 2}')
        self.assertFalse(self.applier.journal('sing-box').exists())

    def test_interrupted_before_rename_is_finished_by_next_run(self):
        self.applier.mark('sing-box')
        original = apply.os.replace

        def crash(source, target):
            if str(target) == str(self.config):
                raise Interrupted()
            original(source, target)
        apply.os.replace = crash
        try:
            self.new.write_text('{"inbounds": [], "v": 2}')
            with self.assertRaises(Interrupted):
                self.applier.install('sing-box', self.new)
        finally:
            apply.os.replace = original
        self.assertEqual(self.config.read_text(), '{"inbounds": [], "v": 1}')
        self.new.write_text('{"inbounds": [], "v": 3}')
        self.applier.install('sing-box', self.new)
        self.assertEqual(len(self.host.ran('restart', 'sing-box')), 2)
        self.assertEqual(self.config.read_text(), '{"inbounds": [], "v": 3}')

    def test_first_install_failure_is_its_own_error(self):
        self.config.unlink()
        failure = self.install('sing-box', '{"inbounds": [], "BROKEN": 1}')
        self.assertEqual(failure.code, 5)
        self.assertIn('first install failed', str(failure))

    def wg(self, *names):
        path = self.root / 'etc/wireguard/wg0.conf'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER + ''.join(peer(name) for name in names))
        return path

    @unittest.skipUnless(shutil.which('openvpn', path='/usr/sbin'),
                         '/usr/sbin/openvpn not installed: the candidate check needs the real binary')
    def test_openvpn_unknown_directive_is_rejected_before_restart(self):
        base = self.root / 'etc/openvpn/server'
        (base / 'pki').mkdir(parents=True)
        (base / 'ccd').mkdir()

        def tool(*argv):
            subprocess.run(argv, cwd=base / 'pki', check=True, capture_output=True)
        for name in ('ca', 'server'):
            tool('openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                 '-nodes', '-keyout', name + '.key', '-out', name + '.crt', '-subj', '/CN=' + name, '-days', '2')
        tool('openvpn', '--genkey', 'secret', 'tls-crypt.key')
        (base / 'pki/index.txt').write_text('')
        (base / 'pki/crlnumber').write_text('01\n')
        (base / 'pki/openssl.cnf').write_text('[ca]\ndefault_ca = x\n[x]\ndatabase = index.txt\n'
                                              'crlnumber = crlnumber\ndefault_md = sha256\n'
                                              'default_crl_days = 2\n')
        tool('openssl', 'ca', '-batch', '-config', 'openssl.cnf', '-keyfile', 'ca.key', '-cert', 'ca.crt',
             '-gencrl', '-out', 'crl.pem')
        working = ('port 1194\nproto udp\ndev tun\nserver 10.8.0.0 255.255.255.0\nca pki/ca.crt\n'
                   'cert pki/server.crt\nkey pki/server.key\ndh none\ntls-crypt pki/tls-crypt.key\n'
                   'crl-verify pki/crl.pem\nclient-config-dir ccd\n')
        self.new.write_text(working)
        self.applier.install('openvpn', self.new)
        self.assertEqual(len(self.host.ran('/usr/sbin/openvpn')), 1)
        for line, word in (('bogus-option 1', 'bogus-option'), ('keepalive 5', 'keepalive')):
            with self.subTest(line=line):
                self.host.calls.clear()
                failure = self.install('openvpn', working + line + '\n')
                self.assertEqual(failure.code, 2)
                self.assertIn('candidate rejected, working config untouched', str(failure))
                self.assertIn(word, str(failure))
                self.assertEqual(self.host.ran('restart'), [])
                self.assertEqual((base / 'server.conf').read_text(), working)
                self.assertEqual(sorted(path.name for path in base.iterdir()), ['ccd', 'pki', 'server.conf'])

if __name__ == '__main__':
    unittest.main()
