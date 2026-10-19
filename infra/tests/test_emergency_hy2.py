"""Emergency issuance contract plus real sing-box/nftables regression in CI."""

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
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
        self.assertFalse(self.hy2.revoked(self.state, 1))

    def test_failed_self_test_retries_same_generation_resumed_one_is_revoked(self):
        self.ready.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'self-test failed'):
            self.start()
        unissued = (self.state / 'hy2/server.json').read_bytes()
        self.ready.return_value = True
        issued = self.start()
        self.assertEqual(self.hy2.read_state(self.state / 'hy2')['generation'], 1)
        self.assertNotEqual((self.state / 'hy2/server.json').read_bytes(), unissued)
        # Credentials already handed out are never minted again under their number.
        self.ready.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'self-test failed'):
            self.start()
        self.assertTrue(self.hy2.revoked(self.state, 1))
        with self.assertRaisesRegex(ValueError, 'revoked'):
            self.start()
        self.assertTrue(issued)

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
            # A pair read across the renewal is refused, with one warning.
            (source / 'fullchain.pem').write_bytes((renewed / 'fullchain.pem').read_bytes())
            with self.assertLogs(self.hy2.LOG, 'WARNING') as logs:
                self.assertFalse(self.hy2.refresh_tls(self.state))
                self.assertFalse(self.hy2.refresh_tls(self.state))
            self.assertEqual(len(logs.records), 1)
            self.assertEqual(sorted(path.name for path in (self.state / 'hy2').glob('incoming-*')), [])
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

    def poll(self, live):
        def fetch(_state):
            if isinstance(live, Exception):
                raise live
            return set(live)
        return self.hy2.poll_sessions(self.state, fetch=fetch)

    def statuses(self, generation=1, **kwargs):
        return self.hy2.connections(self.state, generation, **kwargs)

    def test_connection_status_four_states_per_issuance(self):
        self.start()
        self.assertEqual(self.statuses(), {}, 'no observation yet is unknown')
        self.poll([])
        self.assertEqual(self.statuses(), {1: 'never', 2: 'never'})
        self.poll([1, 7])  # 7 is not issued in this generation
        self.assertEqual(self.statuses(), {1: 'connected', 2: 'never'})
        self.poll([])
        self.assertEqual(self.statuses(), {1: 'seen', 2: 'never'})
        self.poll(ConnectionRefusedError())
        self.assertEqual(self.statuses(), {}, 'source unreachable is unknown')
        self.poll([])
        self.assertEqual(self.statuses(), {1: 'seen', 2: 'never'}, 'flag survives an outage')
        later = self.hy2.time.time() + self.hy2.SESSIONS_STALE + 1
        self.assertEqual(self.statuses(clock=lambda: later), {}, 'poller stopped is unknown')
        self.assertEqual(self.statuses(2), {}, 'another generation is unknown')
        record = __import__('json').loads((self.state / 'hy2/sessions.json').read_text())
        self.assertEqual(set(record), {'generation', 'checked_at', 'ok', 'live', 'seen'})

    def test_new_generation_resets_connection_status(self):
        self.start()
        self.poll([1, 2])
        self.assertEqual(self.statuses(), {1: 'connected', 2: 'connected'})
        self.start(2)
        self.assertEqual(self.statuses(2), {})
        self.poll([])
        self.assertEqual(self.statuses(2), {1: 'never', 2: 'never'})
        self.assertEqual(self.statuses(1), {})
        self.hy2.stop(self.state, runtime=self.runtime)
        self.assertIsNone(self.poll([1]))
        self.assertEqual(self.statuses(2), {}, 'stopped source is unknown')

    def test_stopped_session_watch_writes_nothing_afterwards(self):
        """Stop waits for a pass in flight: the state directory is quiet once it returns."""
        self.start()
        polling, release = threading.Event(), threading.Event()

        def fetch(*_args, **_kwargs):
            polling.set()
            release.wait(5)
            return {1}

        watcher = self.hy2.SessionWatch(self.state, every=0.01, fetch=fetch)
        watcher.start()
        self.assertTrue(polling.wait(5))
        threading.Timer(0.2, release.set).start()
        watcher.stop()
        self.assertFalse(watcher.is_alive(), 'poller outlived stop()')
        written = sorted(path.name for path in (self.state / 'hy2').iterdir())
        time.sleep(0.1)
        self.assertEqual(sorted(path.name for path in (self.state / 'hy2').iterdir()), written)

    def test_session_source_labels_each_device_user(self):
        self.start()
        state = self.hy2.read_state(self.state / 'hy2')
        config = __import__('json').loads((self.state / 'hy2/server.json').read_text())
        self.assertEqual(config['experimental']['clash_api'],
                         {'external_controller': '127.0.0.1:18443', 'secret': state['api_secret']})
        passwords = {user['password'] for user in config['inbounds'][0]['users']}
        self.assertNotIn(state['api_secret'], passwords)
        labels = [rule for rule in config['route']['rules'] if 'auth_user' in rule]
        self.assertEqual(labels, [{'auth_user': [f'g1-d{i}'], 'action': 'route', 'outbound': 'internet'}
                                  for i in (1, 2)])
        # Labels come after the destination rejects and do not widen them.
        self.assertEqual(config['route']['rules'][:2], [{'ip_is_private': True, 'action': 'reject'},
                                                        {'ip_cidr': ['203.0.113.1'], 'action': 'reject'}])
        rules = self.hy2.firewall_rules(1000, 18443, True)
        reply = 'meta skuid 1000 ct direction reply tcp sport 18443 ip daddr 127.0.0.1 accept'
        self.assertIn(reply, rules)
        self.assertLess(rules.index(reply), rules.index('meta skuid 1000 fib daddr type local reject'))

    def test_live_devices_reads_large_answer_in_chunks(self):
        import http.server
        import json
        import threading
        connections = [{'id': str(i), 'chains': ['internet'],
                        'rule': f'auth_user=g3-d{i % 5 + 1} => route(internet)'} for i in range(3000)]
        connections += [{'id': 'p', 'rule': 'final'}, {'id': 'o', 'rule': 'auth_user=g2-d9 => route(internet)'}]
        # Go escapes ">" in JSON strings.
        body = json.dumps({'connections': connections}, separators=(',', ':')).replace('>', '\\u003e').encode()
        self.assertGreater(len(body), 3 * 65536)

        class Clash(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa - HTTPServer callback
                ok = self.path == '/connections' and self.headers['Authorization'] == 'Bearer s3cret'
                self.send_response(200 if ok else 401)
                self.send_header('Content-Length', str(len(body) if ok else 0))
                self.end_headers()
                if ok:
                    self.wfile.write(body)

            def log_message(self, *_args):  # noqa - HTTPServer callback
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Clash)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        state = {'generation': 3, 'options': {'port': server.server_address[1]}, 'api_secret': 's3cret'}
        self.assertEqual(self.hy2.live_devices(state), {1, 2, 3, 4, 5})
        with self.assertRaisesRegex(RuntimeError, '401'):
            self.hy2.live_devices({**state, 'api_secret': 'wrong'})

    def test_poller_runs_in_agent_process_and_survives_errors(self):
        self.start()
        answers = iter([ZeroDivisionError(), {2}])

        def fetch(_state):
            answer = next(answers, {2})
            if isinstance(answer, Exception):
                raise answer
            return answer
        with self.assertLogs(self.hy2.LOG, 'WARNING'):
            watch = self.hy2.watch(self.state, every=.01, fetch=fetch)
            self.addCleanup(watch.stop)
            deadline = self.hy2.time.monotonic() + 5
            while self.statuses() != {1: 'never', 2: 'connected'}:
                self.assertLess(self.hy2.time.monotonic(), deadline)
                self.hy2.time.sleep(.01)
        self.assertIs(self.hy2.watch(self.state), watch)
        self.assertTrue(watch.daemon)
        self.assertEqual(self.hy2.SESSIONS_EVERY, 12)

    def test_poller_stop_waits_for_slow_last_pass(self):
        self.start()
        writing, release = threading.Event(), threading.Event()
        atomic = self.hy2.atomic

        def slow(path, data):
            writing.set()
            release.wait(30)
            atomic(path, data)
        with patch.object(self.hy2, 'atomic', slow):
            watch = self.hy2.watch(self.state, every=.01, fetch=lambda _state: {2})
            self.assertTrue(writing.wait(5))
            # The disk answers later than any fixed wait in stop().
            timer = threading.Timer(11, release.set)
            timer.start()
            self.addCleanup(timer.cancel)
            watch.stop()
            self.assertTrue(release.is_set())
            self.assertFalse(watch.is_alive())

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
