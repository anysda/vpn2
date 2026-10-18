"""HTTPS on the entry IP, explicit renewal windows and ACME failure alerts."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
# Real Caddy (same version as stage 30 CADDY_VER) validates the generated files when present.
CADDY = os.environ.get('CADDY_BIN') or shutil.which('caddy')
# Caddy 2.11.7 journal record for an unreachable ACME directory.
REFUSED = {'level': 'error', 'logger': 'tls.obtain', 'msg': 'could not get certificate from issuer',
           'identifier': '192.0.2.1', 'issuer': '127.0.0.1:1-directory',
           'error': 'registering account [] with server: provisioning client: performing request: '
                    'Get "https://127.0.0.1:1/directory": dial tcp 127.0.0.1:1: connect: connection refused'}


def load_tls():
    library = ROOT / 'infra/lib/vpn2_tls.py'
    if not library.exists():
        raise AssertionError('shared TLS lifecycle is missing: the IP panel stays on HTTP')
    spec = importlib.util.spec_from_file_location('vpn2_tls', library)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render_stage(directory, domain, after=''):
    """Run the TLS part of stage 30 inside the panel transaction, paths moved into a temporary directory."""
    stage = (ROOT / 'infra/scripts/30-frontend.sh').read_text()
    start = stage.index('if [[ -n "${PANEL_DOMAIN:-}" ]]; then', stage.index('TLS_LIB='))
    block = stage[start:stage.index('\nufw_sync_rules', start)]
    Path(directory, 'etc/caddy').mkdir(parents=True, exist_ok=True)
    block = block.replace('/usr/local/lib/vpn2/vpn2_tls.py', f'{ROOT}/infra/lib/vpn2_tls.py')
    for prefix in ('/etc/', '/var/'):
        block = block.replace(prefix, directory + prefix)
    block = (f'source {ROOT}/infra/lib/panel-transaction.sh\npanel_state={directory}/panel-state\n'
             f'systemctl() {{ echo "systemctl $*" >> {directory}/systemctl.log; }}\n{block}\n{after}')
    return subprocess.run(['bash', '-eu', '-c', block],
                          env=dict(os.environ, HOST_TAG='ru', ENTRY_HOST='192.0.2.1', PANEL_DOMAIN=domain),
                          capture_output=True, text=True, timeout=30)


def caddy_validate(test, caddyfile):
    if not CADDY:
        return
    result = subprocess.run([CADDY, 'validate', '--config', str(caddyfile)],
                            capture_output=True, text=True, timeout=30)
    test.assertEqual(result.returncode, 0, result.stderr[-2000:])


class TlsStageTests(unittest.TestCase):
    def test_ip_without_domain_gets_https_site_with_shortlived_profile_and_window(self):
        with tempfile.TemporaryDirectory() as directory:
            result = render_stage(directory, '')
            self.assertEqual(result.returncode, 0, result.stderr)
            caddyfile = (Path(directory) / 'etc/caddy/Caddyfile').read_text()
            policy = (Path(directory) / 'etc/caddy/vpn2-tls.conf').read_text()
            self.assertIn('https://192.0.2.1 {\n    import ' + directory + '/etc/caddy/vpn2-tls.conf', caddyfile)
            self.assertIn('default_sni 192.0.2.1', caddyfile)
            self.assertIn('protocols h1 h2\n', caddyfile)
            self.assertNotIn('h3', caddyfile.split('servers {')[1].split('}')[0])
            self.assertIn('respond @internal 404', caddyfile)
            self.assertIn('profile shortlived', policy)
            self.assertIn('renewal_window_ratio 0.250000000', policy)
            self.assertIn('dir https://acme-v02.api.letsencrypt.org/directory', policy)
            config = json.loads((Path(directory) / 'etc/vpn2/tls/config.json').read_text())
            self.assertEqual(config['output_dir'], directory + '/etc/vpn2/tls/ip')
            # Before the first certificate the panel stays reachable over HTTP.
            self.assertEqual((Path(directory) / 'etc/vpn2/tls/panel/secure').read_text(), 'false\n')
            self.assertIn('import to_panel', (Path(directory) / 'etc/caddy/vpn2-http.conf').read_text())
            caddy_validate(self, Path(directory) / 'etc/caddy/Caddyfile')

    def test_domain_window_is_seven_days_before_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            result = render_stage(directory, 'vpn.example.test')
            self.assertEqual(result.returncode, 0, result.stderr)
            caddyfile = (Path(directory) / 'etc/caddy/Caddyfile').read_text()
            policy = (Path(directory) / 'etc/caddy/vpn2-tls.conf').read_text()
            self.assertIn('vpn.example.test {\n    import ' + directory + '/etc/caddy/vpn2-tls.conf', caddyfile)
            self.assertNotIn('profile', policy)
            ratio = float(policy.split('renewal_window_ratio ')[1].split()[0])
            self.assertAlmostEqual(ratio * 90, 7, places=6)
            caddy_validate(self, Path(directory) / 'etc/caddy/Caddyfile')

    def test_failed_panel_release_restores_previous_tls_snippets(self):
        failure = 'panel_changed=true panel_old=false\ntrap panel_failure EXIT\nfalse'
        names = ('etc/caddy/Caddyfile', 'etc/caddy/vpn2-tls.conf', 'etc/caddy/vpn2-http.conf',
                 'etc/vpn2/tls/config.json', 'etc/vpn2/tls/panel/secure')
        with tempfile.TemporaryDirectory() as directory:
            # First installation fails: none of the new snippets stays behind.
            result = render_stage(directory, '', failure)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual([n for n in names if (Path(directory) / n).exists()], [])
            # Working IP release, then a failed domain release returns the IP snippets and reloads Caddy.
            self.assertEqual(render_stage(directory, '').returncode, 0)
            before = {n: (Path(directory) / n).read_bytes() for n in names}
            result = render_stage(directory, 'vpn.example.test', failure)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual({n: (Path(directory) / n).read_bytes() for n in names}, before)
            self.assertIn('systemctl reload-or-restart caddy', (Path(directory) / 'systemctl.log').read_text())
        # The first certificate is requested only after the panel release is committed.
        stage = (ROOT / 'infra/scripts/30-frontend.sh').read_text()
        self.assertLess(stage.index('panel_guard_files /etc/caddy/Caddyfile'), stage.index('--prepare'))
        self.assertLess(stage.index('\npanel_finish\n'), stage.index('vpn2_tls.py --initial'))
        self.assertLess(stage.index('\npanel_finish\n'), stage.index('enable --now vpn2-tls.timer'))

    def test_panel_reads_secure_flag_from_file_on_every_start(self):
        stage = (ROOT / 'infra/scripts/30-frontend.sh').read_text()
        self.assertIn("NUXT_SESSION_COOKIE_SECURE=$(cat /run/vpn2-tls/secure)", stage)
        self.assertIn('-v /etc/vpn2/tls/panel:/run/vpn2-tls:ro', stage)
        self.assertNotIn('-e NUXT_SESSION_COOKIE_SECURE=', stage)


class TlsTests(unittest.TestCase):
    def setUp(self):
        self.tls = load_tls()

    def test_renewal_windows_follow_real_certificate_lifetime(self):
        for host, lifetime, remaining in [('192.0.2.1', 160 * 3600, 40 * 3600),
                                           ('192.0.2.1', 7 * 86400, 2 * 86400),
                                           ('vpn.example.test', 90 * 86400, 7 * 86400),
                                           ('vpn.example.test', 45 * 86400, 7 * 86400)]:
            config = self.tls.tls_config({'host': host}, lifetime)
            ratio = float(config.split('renewal_window_ratio ')[1].split()[0])
            self.assertAlmostEqual(lifetime * ratio, remaining, delta=.01)
            self.assertEqual('profile shortlived' in config, host == '192.0.2.1')
            self.assertIn('disable_tlsalpn_challenge', config)

    def test_exit_role_is_acme_client_on_port_80_and_leaves_443_free(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['python3', str(ROOT / 'infra/lib/vpn2_tls.py'), '--prepare', '--role', 'exit',
                            '--config', f'{directory}/config.json', '--caddy-dir', directory,
                            '--host', '198.51.100.7', '--cert-group', 'root'], check=True, timeout=30)
            caddyfile = (Path(directory) / 'Caddyfile').read_text()
            self.assertIn('https://198.51.100.7:8443 {\n    bind 127.0.0.1', caddyfile)
            self.assertIn(':80 {', caddyfile)
            self.assertIn('protocols h1 h2\n', caddyfile)
            config = json.loads((Path(directory) / 'config.json').read_text())
            self.assertEqual(config['output_dir'], '/etc/vpn2/tls/ip')
            self.assertNotIn('secure_file', config)
            self.assertNotIn('bot_secret', config)
            self.assertIn('profile shortlived', (Path(directory) / 'vpn2-tls.conf').read_text())
            caddy_validate(self, Path(directory) / 'Caddyfile')

    def test_acme_failure_reaches_bot_queue_with_expiry_and_recovery(self):
        received = []

        class Bot(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa - called by BaseHTTPRequestHandler
                if self.headers['X-Tgbot-Secret'] != 'TEST_ONLY_SECRET':
                    self.send_response(403)
                    self.end_headers()
                    return
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps({'ok': True, 'event_id': len(received)}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):  # noqa - called by BaseHTTPRequestHandler
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Bot)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, 'secret').write_text('TEST_ONLY_SECRET\n')
                config = {'host': '192.0.2.1', 'storage': f'{directory}/storage', 'acme_url': 'https://127.0.0.1:1/directory',
                          'bot_secret': f'{directory}/secret', 'notify_hours': 6,
                          'bot_url': f'http://127.0.0.1:{server.server_port}/event'}
                state_path = Path(directory) / 'state.json'
                with patch.object(self.tls, 'journal', return_value=[REFUSED]):
                    self.assertFalse(self.tls.tick(config, state_path, initial=True))
                with patch.object(self.tls, 'journal', return_value=[REFUSED]):
                    self.tls.tick(config, state_path)
                self.assertEqual(len(received), 1, 'repeat inside notify_hours must be throttled')
                self.assertEqual(received[0]['type'], 'system_alert')
                self.assertEqual(received[0]['subject'], 'TLS: ошибка выпуска/обновления')
                self.assertIn('192.0.2.1: ACME: connection refused; срок истечения: сертификата нет',
                              received[0]['detail'])
                self.assertNotIn('127.0.0.1:1', json.dumps(received))
                self.assertEqual(json.loads(state_path.read_text())['pending'], [])
        finally:
            server.shutdown()
            server.server_close()

    def test_failure_throttle_restart_and_recovery(self):
        config = {'host': '192.0.2.1'}
        state = {}
        cert = {'fingerprint': 'first', 'end': 900000}
        self.tls.observe(config, state, cert, 'ACME: connection refused', 100)
        state = json.loads(json.dumps(state))
        self.tls.observe(config, state, cert, 'ACME: connection refused', 200)
        self.assertEqual(len(state['pending']), 1)
        self.assertIn('1970-01-11 10:00:00 UTC', state['pending'][0]['detail'])
        self.tls.observe(config, state, cert, None, 300)
        self.assertTrue(state['failed'], 'old valid certificate is not recovery')
        self.tls.observe(config, state, cert, 'ACME: timeout', 21700)
        self.assertEqual(len(state['pending']), 2)
        cert = dict(cert, fingerprint='renewed')
        self.tls.observe(config, state, cert, None, 21701)
        self.assertEqual(state['pending'][-1]['subject'], 'TLS: обновлено')
        self.assertFalse(state['failed'])
        self.tls.observe(config, state, cert, None, 21702)
        self.assertEqual(len(state['pending']), 3)

    def test_expiry_remains_in_alert_after_expiration(self):
        state = {}
        self.tls.observe({'host': 'vpn.example.test'}, state,
                         {'fingerprint': 'first', 'end': 900000}, None, 100)
        self.tls.observe({'host': 'vpn.example.test'}, state, None, 'ACME: timeout', 900001)
        self.assertIn('1970-01-11 10:00:00 UTC', state['pending'][0]['detail'])

    def test_error_redacts_secrets_and_ignores_other_sites(self):
        error = {'level': 'error', 'logger': 'tls.obtain', 'identifier': '192.0.2.1',
                 'error': 'urn:ietf:params:acme:error:rejectedIdentifier secret=value https://user:pass@ca/'}
        self.assertEqual(self.tls.safe_error([error], '192.0.2.1'), 'ACME: rejectedIdentifier')
        self.assertIsNone(self.tls.safe_error([error], '192.0.2.2'))
        self.assertEqual(self.tls.safe_error([REFUSED], '192.0.2.1'), 'ACME: connection refused')

    def test_first_failure_queue_survives_unavailable_bot(self):
        state = {}
        self.tls.observe({'host': '192.0.2.1'}, state, None, 'ACME: timeout', 100)
        self.tls.deliver({'bot_secret': '/nonexistent-test-bot-secret'}, state)
        self.tls.deliver({}, state)
        self.assertEqual(len(state['pending']), 1)
        self.tls.observe({'host': '192.0.2.1'}, state,
                         {'fingerprint': 'first', 'end': 900000}, None, 200)
        self.assertEqual([event['subject'] for event in state['pending']],
                         ['TLS: ошибка выпуска/обновления', 'TLS: обновлено'])

    def test_export_validates_san_key_switches_pair_and_prunes_old(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = []
            for index in range(3):
                cert, key = root / f'cert{index}.pem', root / f'key{index}.pem'
                subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-nodes',
                                '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-days', '6',
                                '-subj', '/CN=test', '-addext', 'subjectAltName=IP:192.0.2.1',
                                '-out', str(cert), '-keyout', str(key)], check=True, capture_output=True)
                items.append(self.tls.certificate(cert, key, '192.0.2.1', time.time()))
            self.assertIsNotNone(items[0])
            self.assertIsNone(self.tls.certificate(root / 'cert0.pem', root / 'key0.pem', '192.0.2.2', time.time()))
            self.assertIsNone(self.tls.certificate(root / 'cert0.pem', root / 'key0.pem', '192.0.2.1',
                                                   time.time() + 7 * 86400))
            self.assertIsNone(self.tls.certificate(root / 'cert0.pem', root / 'key1.pem', '192.0.2.1', time.time()))
            config = {'output_dir': str(root / 'ip')}
            for item in items:
                self.tls.export_certificate(config, item)
            self.assertEqual((root / 'ip/fullchain.pem').read_bytes(), (root / 'cert2.pem').read_bytes())
            self.assertEqual((root / 'ip/privkey.pem').read_bytes(), (root / 'key2.pem').read_bytes())
            self.assertEqual((root / 'ip/privkey.pem').stat().st_mode & 0o777, 0o640)
            generations = sorted(path.name for path in (root / 'ip').iterdir() if not path.is_symlink())
            self.assertEqual(generations, sorted([items[1]['fingerprint'], items[2]['fingerprint']]))
            self.tls.export_certificate(config, items[2])
            self.assertEqual(os.readlink(root / 'ip/current'), items[2]['fingerprint'])

    def test_activation_and_retry_preserve_working_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'host': '192.0.2.1', 'caddy_dir': directory,
                      'secure_file': str(root / 'secure'), 'panel_container': 'test-panel'}
            (root / 'vpn2-http.conf').write_text('import to_panel\n')
            (root / 'vpn2-tls.conf').write_text(self.tls.tls_config(config))
            (root / 'secure').write_text('false\n')
            cert = {'start': 0, 'end': 160 * 3600}
            with patch.object(self.tls.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'caddy')):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.tls.activate(config, cert)
            self.assertEqual((root / 'secure').read_text(), 'false\n')
            self.assertEqual((root / 'vpn2-http.conf').read_text(), 'import to_panel\n')
            with patch.object(self.tls.subprocess, 'run') as run:
                self.tls.activate(config, cert)
                self.assertIn(['docker', 'restart', 'test-panel'], [call.args[0] for call in run.call_args_list])
                run.reset_mock()
                self.tls.activate(config, cert)
                run.assert_not_called()
            self.assertEqual((root / 'secure').read_text(), 'true\n')
            self.assertEqual((root / 'vpn2-http.conf').read_text(), 'redir https://192.0.2.1{uri} permanent\n')

    def test_exit_activation_only_updates_window(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'host': '198.51.100.7', 'caddy_dir': directory}
            Path(directory, 'vpn2-tls.conf').write_text(self.tls.tls_config(config))
            with patch.object(self.tls.subprocess, 'run') as run:
                self.tls.activate(config, {'start': 0, 'end': 7 * 86400})
                commands = [call.args[0][0] for call in run.call_args_list]
            self.assertEqual(commands, ['caddy', 'systemctl'])
            self.assertEqual(sorted(os.listdir(directory)), ['vpn2-tls.conf'])
            self.assertIn('renewal_window_ratio 0.285714286', Path(directory, 'vpn2-tls.conf').read_text())


if __name__ == '__main__':
    unittest.main()
