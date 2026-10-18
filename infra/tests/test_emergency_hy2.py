"""Emergency issuance contract plus real sing-box/nftables regression in CI."""

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

from router_support import ROOT, SB


def module():
    path = ROOT / 'infra/emergency/vpn2_emergency/hy2.py'
    spec = importlib.util.spec_from_file_location('emergency_hy2', path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class EmergencyHy2Tests(unittest.TestCase):
    def setUp(self):
        self.hy2 = module()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        self.options = {'server_ip': '203.0.113.1', 'tls_dir': str(self.state / 'missing'),
                        'runtime_dir': str(self.state / 'hy2')}
        self.runtime = Mock()
        self.devices = [{'id': 1, 'name': 'телефон / #1'}, {'id': 2, 'name': 'телефон / #1'}]
        self.ready = patch.object(self.hy2, 'selftest', return_value=True).start()
        self.addCleanup(patch.stopall)

    def start(self, generation=1, devices=None):
        return self.hy2.start(self.state, generation, self.devices if devices is None else devices,
                              options=self.options, runtime=self.runtime)

    def test_one_profile_per_device_with_verified_fallback(self):
        issued = self.start()
        self.assertEqual(set(issued), {1, 2})
        passwords = []
        for item in issued.values():
            uri = urlsplit(item['uri'])
            passwords.append(uri.username)
            self.assertEqual(uri.scheme, 'hysteria2')
            self.assertEqual(uri.port, 18443)
            self.assertEqual(unquote(uri.fragment), self.devices[0]['name'])
            self.assertEqual(Path(item['file']).read_text().strip(), item['uri'])
            self.assertTrue(item['qr_png'].startswith(b'\x89PNG\r\n\x1a\n'))
            image = self.state / 'qr.png'
            image.write_bytes(item['qr_png'])
            decoded = subprocess.run(['zbarimg', '--quiet', '--raw', str(image)],
                                     capture_output=True, check=True).stdout.decode().strip()
            self.assertEqual(decoded, item['uri'])
            pin = parse_qs(uri.query)['pinSHA256'][0]
            der = self.hy2.ssl.PEM_cert_to_DER_cert((self.state / 'hy2/fullchain.pem').read_text())
            self.assertEqual(pin, hashlib.sha256(der).hexdigest())
            self.assertEqual(Path(item['file']).stat().st_mode & 0o777, 0o640)
        self.assertNotEqual(*passwords)
        state = self.hy2.read_state(self.state / 'hy2')
        self.assertNotIn(state['probe_password'], passwords)
        self.ready.assert_called_once_with(self.state, runtime=self.runtime)

    def test_generation_survives_restart_but_revocation_is_durable(self):
        first = self.start()
        self.assertEqual(first, self.start())
        self.runtime.ensure_started.assert_called_once()
        self.hy2.stop(self.state, runtime=self.runtime)
        self.hy2 = module()
        with self.assertRaisesRegex(ValueError, 'revoked'):
            self.start()
        with patch.object(self.hy2, 'selftest', return_value=True):
            second = self.start(2)
        self.assertNotEqual(first[1]['uri'], second[1]['uri'])
        with self.assertRaisesRegex(ValueError, 'revoked'):
            self.start(1)

    def test_no_materials_before_real_readiness(self):
        self.ready.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'self-test failed'):
            self.start()
        self.assertFalse(self.hy2.read_state(self.state / 'hy2')['active'])
        self.assertEqual(list((self.state / 'hy2').glob('device-*.txt')), [])
        self.runtime.stop.assert_called()
        with self.assertRaisesRegex(ValueError, 'revoked'):
            self.start()

    def test_crash_during_rotation_cannot_resurrect_old_files(self):
        self.start()
        old = (self.state / 'hy2/server.json').read_bytes()
        self.runtime.stop.side_effect = RuntimeError('stop failed')
        with self.assertRaisesRegex(RuntimeError, 'stop failed'):
            self.start(2)
        self.assertEqual((self.state / 'hy2/server.json').read_bytes(), old)
        with self.assertRaisesRegex(ValueError, 'inactive'):
            self.hy2.validate(self.state / 'hy2')
        with self.assertRaisesRegex(ValueError, 'revoked'):
            self.start(1)

    def test_no_blocked_expired_or_duplicate_devices(self):
        devices = self.devices + [{'id': 3, 'name': 'blocked', 'allowed': False},
                                  {'id': 4, 'name': 'expired', 'expires_at': '2000-01-01T00:00:00Z'}]
        self.assertEqual(set(self.start(devices=devices)), {1, 2})
        with self.assertRaisesRegex(ValueError, 'new generation'):
            self.start(devices=[self.devices[0]])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.start(2, [self.devices[0], self.devices[0]])

    def test_restored_old_config_is_not_an_active_generation(self):
        self.start()
        previous = (self.state / 'hy2/server.json').read_bytes()
        self.start(2)
        (self.state / 'hy2/server.json').write_bytes(previous)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.start(2)

    def test_trusted_certificate_uri_does_not_disable_tls(self):
        uri = self.hy2.profile({'server_ip': '2001:db8::1', 'port': 18443}, 'a:b', 'device', None)
        self.assertEqual(urlsplit(uri).hostname, '2001:db8::1')
        self.assertEqual(unquote(urlsplit(uri).username), 'a:b')
        self.assertEqual(parse_qs(urlsplit(uri).query), {'sni': ['2001:db8::1']})

    def test_trusted_ip_certificate_and_renewal_keep_credentials(self):
        source = self.state / 'trusted'
        source.mkdir()
        fallback = self.hy2.certificate(source, self.options)
        self.assertTrue(fallback)
        self.options['tls_dir'] = str(source)
        bundle = self.state / 'trust.pem'
        bundle.write_bytes((source / 'fullchain.pem').read_bytes())
        with patch.dict(os.environ, {'SSL_CERT_FILE': str(bundle)}):
            issued = self.start()
            self.assertNotIn('insecure', issued[1]['uri'])
            self.assertNotIn('pinSHA256', issued[1]['uri'])
            self.assertFalse(self.hy2.refresh_tls(self.state))
            renewed = self.state / 'renewed'
            renewed.mkdir()
            self.assertTrue(self.hy2.certificate(renewed, {**self.options, 'tls_dir': str(renewed)}))
            with bundle.open('ab') as trust:
                trust.write((renewed / 'fullchain.pem').read_bytes())
            for name in ('fullchain.pem', 'privkey.pem'):
                (source / name).write_bytes((renewed / name).read_bytes())
            self.assertTrue(self.hy2.refresh_tls(self.state))
            self.assertEqual((self.state / 'hy2/fullchain.pem').read_bytes(),
                             (renewed / 'fullchain.pem').read_bytes())
            self.assertFalse(self.hy2.refresh_tls(self.state))
            self.assertEqual(issued, self.start())
        self.hy2.stop(self.state, runtime=self.runtime)
        self.assertFalse(self.hy2.refresh_tls(self.state))

    def test_fallback_reason_is_logged(self):
        cases = {'missing': 'no certificate at'}
        source = self.state / 'foreign'
        source.mkdir()
        self.hy2.certificate(source, {**self.options, 'server_ip': '198.51.100.7'})
        cases[str(source)] = 'not trusted for 203.0.113.1: '
        (self.state / 'hy2').mkdir()
        for tls_dir, reason in cases.items():
            self.options['tls_dir'] = tls_dir if tls_dir != 'missing' else str(self.state / 'missing')
            with self.assertLogs(self.hy2.LOG, 'WARNING') as logs:
                pin = self.hy2.certificate(self.state / 'hy2', self.hy2.settings(self.options))
            self.assertTrue(pin)
            self.assertIn(reason, logs.output[0])
            self.assertIn('pinSHA256', logs.output[0])

    def test_pinned_generation_never_changes_its_certificate(self):
        self.start()
        before = (self.state / 'hy2/fullchain.pem').read_bytes()
        self.assertFalse(self.hy2.refresh_tls(self.state))
        self.assertEqual(before, (self.state / 'hy2/fullchain.pem').read_bytes())

    def test_server_dns_cannot_be_spoofed_or_local(self):
        # Plain UDP to a public resolver stalled every first lookup ~10 s on a
        # network that injects forged AAAA answers; the node resolver is
        # unreachable for the isolated user by design.
        config = self.hy2.server_config(self.hy2.settings(self.options), [])
        self.assertEqual([server['type'] for server in config['dns']['servers']], ['tls'])
        rules = config['route']['rules']
        self.assertIn({'ip_cidr': ['203.0.113.1'], 'action': 'reject'}, rules)

    def test_independent_unit_is_not_started_at_boot(self):
        service = (ROOT / 'infra/emergency/vpn2-emergency-hy2.service').read_text()
        self.assertIn('User=vpn2-hy2\n', service)
        self.assertIn('hy2.py open', service)
        self.assertIn('hy2.py close', service)
        self.assertNotIn('WantedBy=', service)
        self.assertNotIn('sing-box.service', service)

    def test_sandboxed_agent_controls_unit_without_privilege_escalation(self):
        stage = (ROOT / 'infra/scripts/23-emergency.sh').read_text()
        self.assertIn('NoNewPrivileges=true', stage)  # sudo cannot work inside the agent
        with patch.object(self.hy2.os, 'geteuid', return_value=1000), \
                patch.object(self.hy2, 'command') as run:
            self.hy2.SystemRuntime().ensure_started()
        self.assertEqual(run.call_args.args[0], ['/usr/bin/systemctl', 'start', self.hy2.UNIT])
        # sing-box cannot start without AF_NETLINK, which the agent lacks.
        self.assertNotIn('AF_NETLINK', stage)
        with patch.object(self.hy2.os, 'geteuid', return_value=1000), \
                patch.object(self.hy2, 'command') as run, \
                patch.object(self.hy2, 'probe', side_effect=AssertionError('probe in sandbox')):
            self.assertTrue(self.hy2.SystemRuntime().probe(self.state))
            run.side_effect = RuntimeError('probe failed')
            self.assertFalse(self.hy2.SystemRuntime().probe(self.state))
        self.assertEqual(run.call_args.args[0], ['/usr/bin/systemctl', 'start', self.hy2.PROBE_UNIT])
        probe_unit = (ROOT / 'infra/emergency' / self.hy2.PROBE_UNIT).read_text()
        for needle in ('Type=oneshot', 'User=vpn2-emergency', 'hy2.py probe', 'AF_NETLINK'):
            self.assertIn(needle, probe_unit)
        installer = (ROOT / 'infra/emergency/install-hy2.sh').read_text()
        self.assertNotIn('sudoers', installer)
        for needle in ('org.freedesktop.systemd1.manage-units', '"vpn2-emergency-hy2.service"',
                       '"vpn2-emergency-hy2-probe.service"',
                       'subject.user == "vpn2-emergency"', '"start"', '"stop"', '"restart"'):
            self.assertIn(needle, installer)

    def test_unit_runs_the_module_copy_the_agent_stage_installs(self):
        stage = (ROOT / 'infra/scripts/23-emergency.sh').read_text()
        service = (ROOT / 'infra/emergency/vpn2-emergency-hy2.service').read_text()
        installed = '/opt/vpn2-emergency/vpn2_emergency/'
        self.assertIn(f'"$module" {installed}', stage)
        self.assertEqual(service.count(f'{installed}hy2.py'), 2)
        self.assertNotIn('/usr/local/lib/vpn2-emergency', service)

    @unittest.skipUnless(SB and os.environ.get('RUN_NETNS_TESTS') == '1',
                         'set SING_BOX_BIN and RUN_NETNS_TESTS=1')
    def test_real_isolation_revocation_and_restart(self):
        result = subprocess.run([
            'sudo', '-n', 'unshare', '--net', 'env', f'INFRA_TEST_ROOT={ROOT}',
            f'SING_BOX_BIN={SB}', sys.executable,
            str(Path(__file__).with_name('probe_emergency_hy2.py')),
        ], capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('old credentials rejected after restart; new credentials work', result.stdout)
        print(result.stdout, end='')


if __name__ == '__main__':
    unittest.main(verbosity=2)
