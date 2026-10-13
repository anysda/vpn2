"""Неизмеренная видеопроба не меняет маршрут или серии сетевых результатов."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from test_yt_failover import load_watch


class UnmeasuredTests(unittest.TestCase):
    def test_probe_uses_embedded_video_instead_of_default_forbidden_client(self):
        watch = load_watch()
        downloaded = []

        def run(args, **_kwargs):
            if args == ['/usr/local/bin/anysda-yt-check']:
                return subprocess.CompletedProcess(args, 0)
            if '/opt/anysda-yt-probe/bin/yt-dlp' in args:
                embedded = ('--extractor-args' in args and
                            args[args.index('--extractor-args') + 1] == 'youtube:player_client=web_embedded')
                client = 'WEB_EMBEDDED_PLAYER' if embedded else 'VISIONOS'
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    'url': 'https://rr1.example.googlevideo.com/videoplayback?c=' + client,
                    'format_id': '401', 'protocol': 'https',
                }))
            downloaded.append(args[-1])
            forbidden = 'c=VISIONOS' in args[-1]
            return subprocess.CompletedProcess(args, 22 if forbidden else 0, json.dumps({
                'http_code': 403 if forbidden else 206, 'content_type': 'video/mp4',
                'size_download': 0 if forbidden else 4194304,
                'speed_download': 0 if forbidden else 500000,
            }))

        with patch.object(watch.subprocess, 'run', side_effect=run), \
                patch.object(watch.pwd, 'getpwnam'):
            good, detail = watch.probe()
        self.assertIs(good, True, detail)
        self.assertEqual(len(downloaded), 1)
        self.assertIn('HTTP 206, 4194304 байт', detail)

    def test_stopped_bypass_never_reports_healthy_status(self):
        watch = load_watch()
        with tempfile.TemporaryDirectory() as directory:
            cfg = Path(directory) / 'config.json'
            cfg.write_text('{"outbounds": [{"tag": "youtube-auto"}]}')
            state = Path(directory) / 'state.json'
            with patch.object(watch, 'CONFIG', cfg), patch.object(watch, 'STATE', state), \
                    patch.object(watch, 'notify', return_value=True), \
                    patch.object(watch, 'api', return_value={'now': 'youtube-ru'}) as api, \
                    patch('sys.argv', ['yt-failover', '--status']), \
                    patch('sys.stdout', new_callable=io.StringIO):
                watch.tick(state, lambda: (True, 'TCP video: HTTP 206'))
                for selected in ('youtube-ru', 'foreign-best'):
                    api.return_value = {'now': selected}
                    watch.tick(state, lambda: self.probe(ready=subprocess.CompletedProcess([], 1)))
                    self.assertEqual(watch.main(), 1, selected)
                    self.assertGreater(json.loads(state.read_text())['bad'], 0)

    def probe(self, extraction=None, download=None, ready=None):
        watch = load_watch()
        if extraction is None:
            extraction = subprocess.CompletedProcess([], 0, json.dumps({
                'url': 'https://rr1.example.googlevideo.com/videoplayback?secret=hidden'}))
        if ready is None:
            ready = subprocess.CompletedProcess([], 0)
        with patch.object(watch.subprocess, 'run', side_effect=[ready, extraction, download]), \
                patch.object(watch.pwd, 'getpwnam'):
            return watch.probe()

    def test_extraction_refusals_are_unmeasured(self):
        for error in ("Sign in to confirm you're not a bot", 'Requested format is not available',
                      'No supported JavaScript runtime', 'Unable to extract player response',
                      'Video unavailable', 'HTTP Error 429', 'Connection timed out'):
            with self.subTest(error=error):
                good, detail = self.probe(subprocess.CompletedProcess([], 1, '', error))
                self.assertIsNone(good)
                self.assertTrue(detail)

    def test_untrusted_extractor_output_is_unmeasured_and_not_logged(self):
        for output in ('invalid secret=hidden', 'null', '[]', '{}', '{"url": null}',
                       '{"url": "https://example.org/secret=hidden"}',
                       '{"url": "https://rr1.googlevideo.com/secret=hidden", "http_headers": []}'):
            with self.subTest(output=output):
                good, detail = self.probe(subprocess.CompletedProcess([], 0, output))
                self.assertIsNone(good)
                self.assertNotIn('secret=hidden', detail)

    def test_diagnostic_and_local_errors_are_unmeasured(self):
        for ready in (subprocess.CompletedProcess([], 2), subprocess.CompletedProcess([], 127),
                      FileNotFoundError('secret=hidden'),
                      PermissionError('secret=hidden'), subprocess.TimeoutExpired('check', 35)):
            with self.subTest(ready=ready):
                good, detail = self.probe(ready=ready)
                self.assertIsNone(good)
                self.assertNotIn('secret=hidden', detail)
        for extraction in (KeyError('ytzdiag'), subprocess.TimeoutExpired('yt-dlp', 60)):
            with self.subTest(extraction=extraction):
                self.assertIsNone(self.probe(extraction=extraction)[0])

    def test_403_identifies_cdn_without_leaking_signed_media_url(self):
        good, detail = self.probe(download=subprocess.CompletedProcess(
            [], 22, '{"http_code": 403, "size_download": 0, "speed_download": 0}'))
        self.assertIsNone(good)
        self.assertIn('TCP video: HTTP 403', detail)
        self.assertIn('CDN=rr1.example.googlevideo.com', detail)
        self.assertNotIn('secret=hidden', detail)

    def test_only_video_network_evidence_is_failure(self):
        healthy = {'http_code': 206, 'size_download': 4194304, 'speed_download': 500000,
                   'content_type': 'video/mp4'}
        cases = [(0, healthy, True),
                 (0, dict(healthy, speed_download=249999), False),
                 (0, dict(healthy, size_download=1000), False)]
        cases += [(code, dict(healthy, size_download=1000), False)
                  for code in (18, 28, 52, 55, 56)]
        cases += [(code, {'http_code': 0}, False) for code in (7, 28, 52, 55, 56)]
        # Отказы сервиса/авторизации, неподдерживаемый Range и ошибки curl
        # не доказывают сетевую поломку обхода.
        cases += [(code, dict(healthy, http_code=http), None)
                  for code in (0, 22, 28, 35, 56) for http in (200, 302, 403, 404, 429, 500, 503)]
        cases += [(code, healthy, None) for code in (2, 3, 6, 23, 60, 63, 77, 127)]
        cases += [(code, {'http_code': 0}, None) for code in (6, 60)]
        cases += [(0, dict(healthy, **{key: value}), None) for key, value in (
            ('content_type', 'text/html'), ('size_download', None), ('speed_download', 'fast'),
            ('speed_download', float('nan')), ('size_download', -1), ('http_code', '206'))]
        cases += [(0, stats, None) for stats in ({}, [], None)]
        for code, stats, expected in cases:
            with self.subTest(code=code, stats=stats):
                good, detail = self.probe(download=subprocess.CompletedProcess([], code, json.dumps(stats)))
                self.assertIs(good, expected, detail)
                self.assertNotIn('secret=hidden', detail)
        for download in (FileNotFoundError('curl'), subprocess.TimeoutExpired('curl', 25),
                         subprocess.CompletedProcess([], 0, 'invalid')):
            with self.subTest(download=download):
                self.assertIsNone(self.probe(download=download)[0])

    def test_unknown_freezes_both_counters_and_selector_even_at_threshold(self):
        watch = load_watch()
        for current in ('youtube-ru', 'foreign-best'):
            for good, bad in ((0, 0), (0, 2), (2, 0), (0, 3), (3, 0)):
                with self.subTest(current=current, good=good, bad=bad), \
                        tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'state.json'
                    path.write_text(json.dumps({'good': good, 'bad': bad, 'pending': []}))
                    with patch.object(watch, 'api', return_value={'now': current}) as api, \
                            patch.object(watch, 'notify', return_value=True) as notify:
                        for reason in ('антибот', 'нет форматов', 'нет JS', 'антибот'):
                            watch.tick(path, lambda: (None, reason))
                        self.assertTrue(all(call.args == ('GET',) for call in api.call_args_list))
                        notify.assert_called_once()
                    state = json.loads(path.read_text())
                    self.assertEqual((state['good'], state['bad']), (good, bad))
                    self.assertEqual(state['last_probe'], 'антибот')

    def test_alerts_on_edges_resume_counting_and_retry_delivery(self):
        watch = load_watch()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps({'good': 0, 'bad': 1, 'pending': []}))
            with patch.object(watch, 'api', return_value={'now': 'youtube-ru'}) as api, \
                    patch.object(watch, 'notify', side_effect=[False, True, True, True, True]) as notify:
                for good, detail in ((None, 'антибот'), (None, 'нет JS'), (None, 'нет форматов'),
                                     (False, 'таймаут'), (None, 'антибот'), (True, 'быстро')):
                    watch.tick(path, lambda: (good, detail))
                self.assertEqual(notify.call_count, 5)  # четыре события и повтор доставки
                messages = [call.args[0] for call in notify.call_args_list]
                self.assertEqual(messages[0], messages[1])
                self.assertIn('не измерено', messages[0])
                self.assertIn('возобновлено', messages[2])
                self.assertTrue(all(call.args == ('GET',) for call in api.call_args_list))
            state = json.loads(path.read_text())
            self.assertEqual((state['good'], state['bad']), (1, 0))
            self.assertEqual(state['pending'], [])

    def test_three_bot_refusals_do_not_switch_but_three_timeouts_do(self):
        watch = load_watch()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            current = ['youtube-ru']
            changes = []

            def api(method, body=None):
                if method == 'PUT':
                    current[0] = body['name']
                    changes.append(current[0])
                return {'now': current[0]}

            def sample():
                return self.probe(subprocess.CompletedProcess(
                    [], 1, '', "Sign in to confirm you're not a bot"))

            with patch.object(watch, 'api', side_effect=api), \
                    patch.object(watch, 'notify', return_value=True):
                for _ in range(3):
                    watch.tick(path, sample)
                self.assertEqual(changes, [])
                for _ in range(3):
                    watch.tick(path, lambda: self.probe(download=subprocess.CompletedProcess(
                        [], 28, '{"http_code": 0}')))
                self.assertEqual(changes, ['foreign-best'])
                for _ in range(3):
                    watch.tick(path, sample)
                self.assertEqual(changes, ['foreign-best'])
                for _ in range(3):
                    watch.tick(path, lambda: (True, 'быстро'))
                self.assertEqual(changes, ['foreign-best', 'youtube-ru'])

    def test_status_shows_unknown_reason_and_probe_exit_code_is_distinct(self):
        watch = load_watch()
        with tempfile.TemporaryDirectory() as directory:
            cfg = Path(directory) / 'config.json'
            cfg.write_text('{"outbounds": [{"tag": "youtube-auto"}]}')
            path = Path(directory) / 'state.json'
            with patch.object(watch, 'api', return_value={'now': 'youtube-ru'}), \
                    patch.object(watch, 'notify', return_value=True):
                watch.tick(path, lambda: (None, 'антибот'))
                with patch.object(watch, 'CONFIG', cfg), patch.object(watch, 'STATE', path), \
                        patch('sys.argv', ['yt-failover', '--status']), \
                        patch('sys.stdout', new_callable=io.StringIO) as output:
                    self.assertEqual(watch.main(), 2)
                    self.assertIn('не измерено', output.getvalue())
                    self.assertIn('антибот', output.getvalue())
            for flag in ('--probe-only', '--probe-quic'):
                with patch.object(watch, 'probe', return_value=(None, 'антибот')), \
                        patch('sys.argv', ['yt-failover', flag]), \
                        patch('sys.stdout', new_callable=io.StringIO) as output:
                    self.assertEqual(watch.main(), 2)
                    self.assertIn('не измерено', output.getvalue())

    def test_status_requires_recent_measured_state_and_known_selector(self):
        watch = load_watch()
        with tempfile.TemporaryDirectory() as directory:
            cfg = Path(directory) / 'config.json'
            cfg.write_text('{"outbounds": [{"tag": "youtube-auto"}]}')
            state_path = Path(directory) / 'state.json'
            measured = {'good': 1, 'bad': 0, 'last_probe': 'TCP video: HTTP 206',
                        'checked_at': time.time(), 'unmeasured': False}
            cases = [(measured, 0), (dict(measured, good=0, bad=3), 1),
                     (dict(measured, good=0), 2), (dict(measured, last_probe=''), 2),
                     (dict(measured, checked_at=time.time() - 601), 2),
                     (dict(measured, checked_at=time.time() + 60), 2),
                     (dict(measured, checked_at=None), 2),
                     (dict(measured, good=-1), 2), (dict(measured, good=True), 2),
                     (dict(measured, unmeasured=True, last_probe='TCP video: HTTP 403'), 2),
                     ({}, 2), ([], 2)]
            with patch.object(watch, 'CONFIG', cfg), patch.object(watch, 'STATE', state_path), \
                    patch.object(watch, 'api', return_value={'now': 'youtube-ru'}) as api, \
                    patch.object(watch, 'probe') as probe, \
                    patch('sys.argv', ['yt-failover', '--status']), \
                    patch('sys.stdout', new_callable=io.StringIO):
                for state, expected in cases:
                    with self.subTest(state=state):
                        state_path.write_text(json.dumps(state))
                        self.assertEqual(watch.main(), expected)
                        self.assertEqual(json.loads(state_path.read_text()), state)
                state_path.unlink()
                self.assertEqual(watch.main(), 2)
                api.return_value = {'now': 'unknown'}
                self.assertEqual(watch.main(), 1)
                cfg.write_text('{"outbounds": []}')
                self.assertEqual(watch.main(), 2)
                probe.assert_not_called()
                self.assertTrue(all(call.args == ('GET',) for call in api.call_args_list))


if __name__ == '__main__':
    unittest.main()
