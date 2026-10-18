"""Проверки маршрута и настоящего контроллера с подставными сетью/видеопробой."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from router_support import exit_cert
from test_deploy import ROOT, source


def load_watch():
    path = ROOT / 'infra/lib/yt-failover.py'
    spec = importlib.util.spec_from_file_location('yt_failover', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class YoutubeFailoverTests(unittest.TestCase):
    def router(self, mode, quic):
        env = dict(os.environ, EXIT_TAGS='test', DOMAIN_TEST='exit.example.org',
                   TEST_DIRECT='fake', TEST_WARP='fake', TEST_MGMT='fake', TEST_OBFS='fake',
                   MGMT_IP_TEST='192.0.2.2', TEST_HY2_CERT_PATH=exit_cert(),
                   HY2_DIRECT_PORT='443', HY2_WARP_PORT='444',
                   HY2_MGMT_PORT='445', WG_OUT_IFACE='eth0', RU_CLASH_SECRET='fake',
                   MGMT_IP='127.0.0.1', YT_ROUTE=mode, YT_QUIC=quic)
        result = subprocess.run(['python3', str(ROOT / 'infra/lib/gen-router-config.py')],
                                env=env, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_youtube_routes_through_selector_in_both_quic_modes(self):
        for quic in ('block', 'allow'):
            with self.subTest(quic=quic):
                cfg = self.router('zapret', quic)
                outs = {o['tag']: o for o in cfg['outbounds']}
                self.assertIn('youtube-auto', outs, 'нет запасного пути YouTube')
                self.assertEqual(outs['youtube-auto']['outbounds'], ['youtube-ru', 'foreign-best'])
                self.assertEqual(outs['youtube-auto']['default'], 'youtube-ru')
                self.assertTrue(outs['youtube-auto']['interrupt_exist_connections'])
                self.assertEqual(outs['youtube-ru']['routing_mark'], 256)
                rules = [r for r in cfg['route']['rules'] if r.get('outbound') == 'youtube-auto']
                self.assertEqual(len(rules), 2)
                rejects = [r for r in cfg['route']['rules'] if r.get('action') == 'reject'
                           and any(c.get('network') == 'udp' and c.get('port') == [443]
                                   for c in r.get('rules', [r]))]
                self.assertEqual(len(rejects), 2 if quic == 'block' else 0)

    def test_off_and_direct_do_not_gain_watchdog_selector(self):
        for mode in ('off', 'direct'):
            cfg = self.router(mode, 'allow')
            self.assertNotIn('youtube-auto', [o['tag'] for o in cfg['outbounds']])

    def test_failure_recovery_and_single_failure(self):
        watch = load_watch()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'state.json'
            current = ['youtube-ru']
            changes = []

            def api(method, body=None):
                if method == 'PUT':
                    current[0] = body['name']
                    changes.append(current[0])
                return {'now': current[0]}

            with patch.object(watch, 'api', side_effect=api), \
                    patch.object(watch, 'notify', return_value=True) as notify:
                for good in (False, True, False, False):
                    watch.tick(state_path, lambda: (good, 'sample'))
                self.assertEqual(changes, [])
                watch.tick(state_path, lambda: (False, 'slow'))
                self.assertEqual(changes, ['foreign-best'])
                # Каждый tick читает состояние с диска: отдельные oneshot-процессы.
                for good in (True, False, True, True):
                    watch.tick(state_path, lambda: (good, 'sample'))
                self.assertEqual(current[0], 'foreign-best')
                watch.tick(state_path, lambda: (True, 'fast'))
                self.assertEqual(changes, ['foreign-best', 'youtube-ru'])
                self.assertEqual(notify.call_count, 2)

    def test_api_failure_does_not_claim_switch_and_retries(self):
        watch = load_watch()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'state.json'
            state_path.write_text(json.dumps({'bad': 2, 'good': 0, 'pending': []}))
            with patch.object(watch, 'api', side_effect=[{'now': 'youtube-ru'}, OSError('offline')]), \
                    patch.object(watch, 'notify') as notify:
                with self.assertRaises(OSError):
                    watch.tick(state_path, lambda: (False, 'slow'))
                notify.assert_not_called()
            with patch.object(watch, 'api', side_effect=[{'now': 'youtube-ru'}, {}, {'now': 'foreign-best'}]), \
                    patch.object(watch, 'notify', return_value=True) as notify:
                watch.tick(state_path, lambda: (False, 'slow'))
                notify.assert_called_once()

    def test_alert_failure_is_retried_without_another_switch(self):
        watch = load_watch()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'state.json'
            state_path.write_text(json.dumps({'bad': 2, 'good': 0, 'pending': []}))
            with patch.object(watch, 'api', side_effect=[{'now': 'youtube-ru'}, {}, {'now': 'foreign-best'}]), \
                    patch.object(watch, 'notify', return_value=False):
                watch.tick(state_path, lambda: (False, 'slow'))
            self.assertEqual(len(json.loads(state_path.read_text())['pending']), 1)
            with patch.object(watch, 'api', return_value={'now': 'foreign-best'}) as api, \
                    patch.object(watch, 'notify', return_value=True) as notify:
                watch.tick(state_path, lambda: (False, 'slow'))
                self.assertEqual(api.call_count, 1)
                notify.assert_called_once()
            self.assertEqual(json.loads(state_path.read_text())['pending'], [])

    def test_media_verdict_rejects_slow_short_html_and_partial_transfer(self):
        watch = load_watch()
        healthy = {'http_code': 206, 'size_download': 4194304, 'speed_download': 500000,
                   'content_type': 'video/mp4'}
        self.assertTrue(watch.media_ok(0, healthy))
        for key, value in (('speed_download', 249999), ('size_download', 1000),
                           ('content_type', 'text/html'), ('http_code', 403)):
            self.assertFalse(watch.media_ok(0, dict(healthy, **{key: value})))
        self.assertFalse(watch.media_ok(28, healthy))

    def test_media_probe_uses_diag_user_and_tcp_even_when_quic_allowed(self):
        watch = load_watch()
        responses = [subprocess.CompletedProcess([], 0),
                     subprocess.CompletedProcess([], 0, json.dumps({
                         'url': 'https://rr1.example.googlevideo.com/videoplayback?expire=1',
                         'http_headers': {'User-Agent': 'probe'}})),
                     subprocess.CompletedProcess([], 0, json.dumps({
                         'http_code': 206, 'size_download': 4194304, 'speed_download': 500000,
                         'content_type': 'video/mp4'}))]
        with patch.object(watch.subprocess, 'run', side_effect=responses) as run, \
                patch.object(watch.pwd, 'getpwnam') as user:
            user.return_value.pw_uid = 42
            user.return_value.pw_gid = 42
            good, _ = watch.probe()
            self.assertTrue(good)
            for call in run.call_args_list[1:]:
                self.assertEqual(call.args[0][:4], ['setpriv', '--reuid=42', '--regid=42', '--clear-groups'])
            curl = run.call_args.args[0]
            self.assertIn('--http1.1', curl)
            self.assertIn('0-4194303', curl)

    def test_install_and_teardown_own_timer(self):
        stage = source('infra/scripts/19-yt-zapret.sh')
        self.assertIn('systemctl enable --now anysda-yt-failover.timer', stage)
        self.assertIn('systemctl disable --now anysda-yt-failover.timer', stage)
        self.assertIn('systemctl stop anysda-yt-failover.service', stage)
        self.assertIn('yt-failover.py', source('infra/deploy.sh'))

    def test_quic_diagnostic_cannot_silently_fall_back_to_tcp(self):
        watch = load_watch()
        responses = [subprocess.CompletedProcess([], 0),
                     subprocess.CompletedProcess([], 0, json.dumps({
                         'url': 'https://rr1.example.googlevideo.com/videoplayback'})),
                     subprocess.CompletedProcess([], 2, ''),
                     subprocess.CompletedProcess([], 1, '', 'no formats')]
        with patch.object(watch.subprocess, 'run', side_effect=responses) as run, \
                patch.object(watch.os, 'chown'), \
                patch.object(watch.pwd, 'getpwnam') as user:
            user.return_value.pw_uid = user.return_value.pw_gid = 42
            good, detail = watch.probe(quic=True)
            self.assertFalse(good)
            self.assertIn('--http3-only', run.call_args_list[2].args[0])
            self.assertIn('curl=2', detail)

    def test_login_challenge_is_not_reported_as_successful_bypass(self):
        watch = load_watch()
        with patch.object(watch.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 1, '', 'Sign in to confirm you are not a bot'),
                subprocess.CompletedProcess([], 1, '', 'Sign in to confirm you are not a bot'),
        ]), patch.object(watch.pwd, 'getpwnam'), patch.object(watch.os, 'chown'):
            good, detail = watch.probe()
            self.assertFalse(good)
            self.assertIn('обход не измерен', detail)

    def test_status_only_reads_actual_selector(self):
        watch = load_watch()
        with tempfile.TemporaryDirectory() as directory:
            cfg = Path(directory) / 'config.json'
            cfg.write_text(json.dumps(self.router('zapret', 'allow')))
            with patch.object(watch, 'CONFIG', cfg), patch.object(watch, 'STATE', cfg.parent / 'absent'), \
                    patch.object(watch, 'api', return_value={'now': 'foreign-best'}) as api, \
                    patch.object(watch, 'probe') as probe, patch('builtins.print'), \
                    patch('sys.argv', ['yt-failover', '--status']):
                self.assertEqual(watch.main(), 2)
                api.assert_called_once_with('GET')
                probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
