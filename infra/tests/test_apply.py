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


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
spec = importlib.util.spec_from_file_location('apply', ROOT / 'infra/control/apply.py')
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

    def test_rollback_after_revocation_does_not_return_the_device(self):
        path = self.wg('a', 'b')
        self.applier.mark('wg')
        path.write_text(HEADER.replace('51820', '51821') + peer('b'))   # a revoked since
        with self.assertRaises(apply.Failed) as caught:
            self.applier.rollback('wg')
        failure = caught.exception
        self.assertEqual(failure.code, 3)
        self.assertIn('ListenPort = 51820', path.read_text())
        self.assertNotIn(KEY['a'], path.read_text())
        self.assertIn(KEY['b'], path.read_text())
        loaded = self.host.calls[[argv for argv, _ in self.host.calls].index(
            ['/usr/bin/wg', 'syncconf', 'wg0', '/dev/stdin'])][1]
        self.assertNotIn(KEY['a'], loaded)
        self.assertIn(KEY['b'], loaded)
        self.assertNotIn(KEY['a'], self.applier.good('wg').read_text())

    def test_wg_rollback_with_every_device_revoked_loads_the_interface(self):
        path = self.wg('a')
        self.applier.mark('wg')
        path.write_text(HEADER.replace('51820', '51821'))
        with self.assertRaises(apply.Failed) as caught:
            self.applier.rollback('wg')
        self.assertEqual(caught.exception.code, 3)
        self.assertNotIn(KEY['a'], path.read_text())

    def test_wg_candidate_cannot_bring_back_peers(self):
        path = self.wg('b')
        self.new.write_text(HEADER + peer('a') + peer('c'))
        self.applier.install('wg', self.new)
        self.assertNotIn(KEY['a'], path.read_text())
        self.assertIn(KEY['b'], path.read_text())

    def test_openvpn_rollback_that_drops_the_crl_is_an_explicit_error(self):
        base = self.root / 'etc/openvpn/server'
        (base / 'pki').mkdir(parents=True)
        (base / 'ccd').mkdir()
        (base / 'pki/crl.pem').write_text('crl')
        weak = 'port 1194\nclient-config-dir ccd\n'
        (base / 'server.conf').write_text(weak)
        self.applier.mark('openvpn')
        strict = weak + 'crl-verify pki/crl.pem\n'
        (base / 'server.conf').write_text(strict)
        with self.assertRaises(apply.Failed) as caught:
            self.applier.rollback('openvpn')
        self.assertEqual(caught.exception.code, 4)
        self.assertIn('restore failed: crl-verify must be', str(caught.exception))
        self.assertEqual((base / 'server.conf').read_text(), strict)

    @unittest.skipUnless(shutil.which('openvpn', path='/usr/sbin'), 'openvpn not installed')
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
        self.host.calls.clear()
        failure = self.install('openvpn', working + 'bogus-option 1\n')
        self.assertEqual(failure.code, 2)
        self.assertIn('candidate rejected, working config untouched', str(failure))
        self.assertIn('bogus-option', str(failure))
        self.assertEqual(self.host.ran('restart'), [])
        self.assertEqual((base / 'server.conf').read_text(), working)
        self.assertEqual(sorted(path.name for path in base.iterdir()), ['ccd', 'pki', 'server.conf'])

    def test_nft_candidate_may_touch_only_its_table(self):
        self.new.write_text('table inet anysda_ytquic {}\ntable inet filter { chain input {} }\n')
        with self.assertRaises(apply.Failed) as caught:
            self.applier.nft('anysda_ytquic', self.new, 60)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.host.ran('/usr/sbin/nft', '-f'), [])

    def test_nft_candidate_failing_check_is_rejected_before_the_timer(self):
        self.new.write_text('table inet anysda_ytquic { chain c { tcp dport 22 drop SYNTAX } }\n')
        with self.assertRaises(apply.Failed) as caught:
            self.applier.nft('anysda_ytquic', self.new, 60)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('candidate rejected', str(caught.exception))
        self.assertEqual(self.host.ran('systemd-run'), [])

    def test_nft_change_is_armed_with_a_local_restore_timer(self):
        self.new.write_text('table inet anysda_ytquic\ndelete table inet anysda_ytquic\n'
                            'table inet anysda_ytquic { chain c { tcp dport 22 drop } }\n')
        self.applier.nft('anysda_ytquic', self.new, 60)
        timer = self.host.ran('systemd-run')
        self.assertEqual(timer[0][:4], ['systemd-run', '--unit', 'vpn2-apply-nft-anysda_ytquic',
                                        '--on-active=60'])
        restore = Path(timer[0][-1]).read_text()
        self.assertTrue(restore.startswith('table inet anysda_ytquic\ndelete table inet anysda_ytquic\n'))
        self.assertNotIn('filter', restore)
        self.assertEqual(self.host.ran('vpn2-apply-nft-anysda_ytquic.timer', 'stop')[-1][0], 'systemctl')
        calls = len(self.host.calls)
        self.applier.confirm('anysda_ytquic')
        self.assertIn(['systemctl', 'stop', 'vpn2-apply-nft-anysda_ytquic.timer'],
                      [argv for argv, _ in self.host.calls[calls:]])

    def test_nft_failed_load_restores_the_table_at_once(self):
        self.host.nft_fails = True
        self.new.write_text('table inet anysda_ytquic { }\n')
        with self.assertRaises(apply.Failed) as caught:
            self.applier.nft('anysda_ytquic', self.new, 60)
        self.assertEqual(caught.exception.code, 3)
        loads = [argv[-1] for argv in self.host.ran('/usr/sbin/nft', '-f')]
        self.assertTrue(loads[-1].endswith('nft-anysda_ytquic.restore'))


if __name__ == '__main__':
    unittest.main()
