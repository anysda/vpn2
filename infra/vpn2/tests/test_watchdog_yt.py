"""Сторож YouTube (`vpn2 watchdog yt`): проба видео, счётчики, переключение маршрута, алерты.

Каждый тик - отдельный процесс: модуль грузится заново, общее только state-файл.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vpn2 import resources
from vpn2.watchdog import yt

CHECK = ['/usr/local/bin/anysda-yt-check']
YTDLP = '/opt/anysda-yt-probe/bin/yt-dlp'
URL = 'https://rr1.example.googlevideo.com/videoplayback'
GOOD = {'http_code': 206, 'size_download': 4194304, 'speed_download': 500000,
        'content_type': 'video/mp4', 'url_effective': URL}
STEP = 300  # период systemd-таймера
ROOT = Path(__file__).resolve().parents[3]


def load():
    spec = importlib.util.spec_from_file_location('yt_watch', yt.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def done(code=0, out=''):
    return subprocess.CompletedProcess([], code, out if isinstance(out, str) else json.dumps(out), '')


def fake_run(ytdlp, curl=None, calls=None):
    """subprocess.run пробы: ytdlp(аргументы) и curl(аргументы) дают CompletedProcess."""
    def run(args, **_):
        if calls is not None:
            calls.append(args)
        if args == CHECK:
            return subprocess.CompletedProcess(args, 0)
        if YTDLP in args:
            return ytdlp(args)
        return curl(args) if curl else subprocess.CompletedProcess(args, 0, json.dumps(GOOD))
    return run


class Selector:
    """Селектор youtube-auto через clash API: текущее значение и история смен."""

    def __init__(self):
        self.now, self.changes = 'youtube-ru', []

    def __call__(self, method, body=None):
        if method == 'PUT':
            self.now = body['name']
            self.changes.append(self.now)
        return {'now': self.now}


def tick(watch, run, state=None):
    """Один тик с пробой на подставном subprocess.run: (state после, смены маршрута, строки журнала)."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'state.json'
        if state is not None:
            path.write_text(json.dumps(state))
        selector = Selector()
        user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        with patch.object(watch.subprocess, 'run', side_effect=run), \
                patch.object(watch.pwd, 'getpwnam', return_value=user), \
                patch.object(watch, 'api', side_effect=selector), \
                patch.object(watch, 'notify', return_value=True), \
                patch('builtins.print') as printed:
            watch.tick(path, watch.probe)
        lines = [' '.join(str(arg) for arg in call.args) for call in printed.call_args_list]
        return json.loads(path.read_text()), selector.changes, lines


def probe(extraction=None, download=None, ready=None):
    """watch.probe() с ответами проверки обхода, yt-dlp и curl (или исключениями)."""
    watch = load()
    extraction = extraction or done(0, {'url': URL + '?secret=hidden'})
    ready = ready or subprocess.CompletedProcess([], 0)

    def run(args, **_kwargs):
        result = ready if args == CHECK else extraction if YTDLP in args else download
        if isinstance(result, Exception):
            raise result
        return result

    with patch.object(watch.subprocess, 'run', side_effect=run), patch.object(watch.os, 'chown'), \
            patch.object(watch.pwd, 'getpwnam'):
        return watch.probe()


class SwitchTests(unittest.TestCase):
    def test_failure_recovery_and_single_failure(self):
        watch = load()
        selector = Selector()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch.object(watch, 'api', side_effect=selector), \
                    patch.object(watch, 'notify', return_value=True) as notify:
                for good in (False, True, False, False):
                    watch.tick(path, lambda: (good, 'sample'))
                self.assertEqual(selector.changes, [])
                watch.tick(path, lambda: (False, 'slow'))
                self.assertEqual(selector.changes, ['foreign-best'])
                for good in (True, False, True, True):
                    watch.tick(path, lambda: (good, 'sample'))
                self.assertEqual(selector.now, 'foreign-best')
                watch.tick(path, lambda: (True, 'fast'))
                self.assertEqual(selector.changes, ['foreign-best', 'youtube-ru'])
                self.assertEqual(notify.call_count, 2)

    def test_api_reads_and_switches_selector_through_clash_api(self):
        watch = load()
        seen = []

        class Response:
            def __init__(self, raw):
                self.raw = raw

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return self.raw

        class Opener:
            def open(self, req, timeout):
                seen.append((req.get_method(), req.full_url, req.data, timeout))
                return Response(b'{"now": "youtube-ru"}' if req.get_method() == 'GET' else b'')

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'experimental': {'clash_api': {
                'external_controller': '127.0.0.1:9090', 'secret': 's'}}}))
            with patch.object(watch, 'CONFIG', config), patch.object(watch, 'OPENER', Opener()):
                self.assertEqual(watch.api('GET'), {'now': 'youtube-ru'})
                self.assertEqual(watch.api('PUT', {'name': 'foreign-best'}), {})
        self.assertEqual(seen, [
            ('GET', 'http://127.0.0.1:9090/proxies/youtube-auto', None, 5),
            ('PUT', 'http://127.0.0.1:9090/proxies/youtube-auto', b'{"name": "foreign-best"}', 5)])

    def test_api_failure_does_not_claim_switch_and_retries(self):
        watch = load()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps({'bad': 2, 'good': 0, 'pending': []}))
            with patch.object(watch, 'api', side_effect=[{'now': 'youtube-ru'}, OSError('offline')]), \
                    patch.object(watch, 'notify') as notify:
                with self.assertRaises(OSError):
                    watch.tick(path, lambda: (False, 'slow'))
                notify.assert_not_called()
            with patch.object(watch, 'api', side_effect=[{'now': 'youtube-ru'}, {}, {'now': 'foreign-best'}]), \
                    patch.object(watch, 'notify', return_value=True) as notify:
                watch.tick(path, lambda: (False, 'slow'))
                notify.assert_called_once()

    def test_alert_failure_is_retried_without_another_switch(self):
        watch = load()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps({'bad': 2, 'good': 0, 'pending': []}))
            with patch.object(watch, 'api', side_effect=[{'now': 'youtube-ru'}, {}, {'now': 'foreign-best'}]), \
                    patch.object(watch, 'notify', return_value=False):
                watch.tick(path, lambda: (False, 'slow'))
            self.assertEqual(len(json.loads(path.read_text())['pending']), 1)
            with patch.object(watch, 'api', return_value={'now': 'foreign-best'}) as api, \
                    patch.object(watch, 'notify', return_value=True) as notify:
                watch.tick(path, lambda: (False, 'slow'))
                self.assertEqual(api.call_count, 1)
                notify.assert_called_once()
            self.assertEqual(json.loads(path.read_text())['pending'], [])

    def assert_failover(self, watch):
        """Три провала подряд, каждый измерен: маршрут сменён ровно на третьем."""
        selector = Selector()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch.object(watch, 'api', side_effect=selector), \
                    patch.object(watch, 'notify', return_value=True), patch('builtins.print'):
                for hit in range(1, 4):
                    watch.tick(path, watch.probe)
                    state = json.loads(path.read_text())
                    self.assertEqual((state['bad'], state['unmeasured']), (hit, False))
                    self.assertEqual(selector.changes, ['foreign-best'] if hit == 3 else [])

    def test_broken_bypass_switches_after_three_failures(self):
        # anysda-yt-check: 1 - служба стоит, нет очереди, таблицы, правил или пакетов.
        watch = load()
        with patch.object(watch.subprocess, 'run', return_value=subprocess.CompletedProcess(CHECK, 1)) as run:
            self.assert_failover(watch)
        self.assertEqual([call.args[0] for call in run.call_args_list], [CHECK] * 3)

    def test_missing_diagnostic_tools_are_unmeasured(self):
        # anysda-yt-check: 2 - нечем проверить (нет curl, nft, vpn2, setpriv; DNS, сертификат, отказ HTTP).
        self.assertIsNone(probe(ready=subprocess.CompletedProcess(CHECK, 2))[0])

    def test_googlevideo_handshake_reset_and_truncated_transfer_switch(self):
        cases = [(rc, {'http_code': 0}) for rc in (7, 28, 35, 52, 55, 56)]
        cases += [(rc, {'http_code': 206, 'content_type': 'video/mp4', 'size_download': 16384, 'speed_download': 819})
                  for rc in (0, 18, 28, 55, 56)]
        for rc, stats in cases:
            with self.subTest(rc=rc, stats=stats):
                watch = load()
                responses = [done(), done(0, {'url': URL}), done(rc, stats)]
                with patch.object(watch.subprocess, 'run', side_effect=responses * 3), \
                        patch.object(watch.pwd, 'getpwnam'):
                    self.assert_failover(watch)

    def test_three_bot_refusals_do_not_switch_but_three_timeouts_do(self):
        watch = load()
        selector = Selector()
        def bot():
            return probe(subprocess.CompletedProcess([], 1, '', "Sign in to confirm you're not a bot"))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch.object(watch, 'api', side_effect=selector), patch.object(watch, 'notify', return_value=True):
                for sample, wanted in ((bot, []), (lambda: probe(download=done(28, {'http_code': 0})), ['foreign-best']),
                                       (bot, ['foreign-best']), (lambda: (True, 'быстро'), ['foreign-best', 'youtube-ru'])):
                    for _ in range(3):
                        watch.tick(path, sample)
                    self.assertEqual(selector.changes, wanted)

    def test_unknown_freezes_both_counters_and_selector_even_at_threshold(self):
        watch = load()
        for current in ('youtube-ru', 'foreign-best'):
            for good, bad in ((0, 0), (0, 2), (2, 0), (0, 3), (3, 0)):
                with self.subTest(current=current, good=good, bad=bad), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'state.json'
                    path.write_text(json.dumps({'good': good, 'bad': bad, 'pending': []}))
                    with patch.object(watch, 'api', return_value={'now': current}) as api, \
                            patch.object(watch, 'notify', return_value=True) as notify:
                        for reason in ('антибот', 'нет форматов', 'нет JS', 'антибот'):
                            watch.tick(path, lambda: (None, reason))
                        self.assertTrue(all(call.args == ('GET',) for call in api.call_args_list))
                        notify.assert_not_called()
                    state = json.loads(path.read_text())
                    self.assertEqual((state['good'], state['bad'], state['last_probe']), (good, bad, 'антибот'))


class ProbeTests(unittest.TestCase):
    def test_media_verdict_rejects_slow_short_html_and_partial_transfer(self):
        watch = load()
        healthy = {'http_code': 206, 'size_download': 4194304, 'speed_download': 500000, 'content_type': 'video/mp4'}
        self.assertTrue(watch.media_ok(0, healthy))
        for key, value in (('speed_download', 249999), ('size_download', 1000),
                           ('content_type', 'text/html'), ('http_code', 403)):
            self.assertFalse(watch.media_ok(0, dict(healthy, **{key: value})))
        self.assertFalse(watch.media_ok(28, healthy))

    def test_media_probe_uses_diag_user_and_tcp_even_when_quic_allowed(self):
        watch = load()
        responses = [done(), done(0, {'url': URL + '?expire=1', 'http_headers': {'User-Agent': 'probe'}}), done(0, GOOD)]
        with patch.object(watch.subprocess, 'run', side_effect=responses) as run, \
                patch.object(watch.pwd, 'getpwnam') as user:
            user.return_value.pw_uid = user.return_value.pw_gid = 42
            self.assertTrue(watch.probe()[0])
        for call in run.call_args_list[1:]:
            self.assertEqual(call.args[0][:4], ['setpriv', '--reuid=42', '--regid=42', '--clear-groups'])
        self.assertIn('--http1.1', run.call_args.args[0])
        self.assertIn('0-4194303', run.call_args.args[0])

    def test_probe_uses_embedded_video_instead_of_default_forbidden_client(self):
        watch = load()
        downloaded = []

        def run(args, **_kwargs):
            if args == CHECK:
                return subprocess.CompletedProcess(args, 0)
            if YTDLP in args:
                embedded = args[args.index('--extractor-args') + 1] == 'youtube:player_client=web_embedded' \
                    if '--extractor-args' in args else False
                return done(0, {'url': URL + '?c=' + ('WEB_EMBEDDED_PLAYER' if embedded else 'VISIONOS'),
                                'format_id': '401', 'protocol': 'https'})
            downloaded.append(args[-1])
            forbidden = 'c=VISIONOS' in args[-1]
            return done(22 if forbidden else 0, {'http_code': 403 if forbidden else 206, 'content_type': 'video/mp4',
                                                  'size_download': 0 if forbidden else 4194304,
                                                  'speed_download': 0 if forbidden else 500000})

        with patch.object(watch.subprocess, 'run', side_effect=run), patch.object(watch.pwd, 'getpwnam'):
            good, detail = watch.probe()
        self.assertIs(good, True, detail)
        self.assertEqual(len(downloaded), 1)
        self.assertIn('HTTP 206, 4194304 байт', detail)

    def test_login_challenge_is_not_reported_as_successful_bypass(self):
        bot = subprocess.CompletedProcess([], 1, '', 'Sign in to confirm you are not a bot')
        good, detail = probe(extraction=bot)
        self.assertFalse(good)
        self.assertIn('обход не измерен', detail)

    def test_extraction_refusals_are_unmeasured(self):
        for error in ("Sign in to confirm you're not a bot", 'Requested format is not available',
                      'No supported JavaScript runtime', 'Unable to extract player response',
                      'Video unavailable', 'HTTP Error 429', 'Connection timed out'):
            with self.subTest(error=error):
                good, detail = probe(subprocess.CompletedProcess([], 1, '', error))
                self.assertIsNone(good)
                self.assertTrue(detail)

    def test_untrusted_extractor_output_is_unmeasured_and_not_logged(self):
        for output in ('invalid secret=hidden', 'null', '[]', '{}', '{"url": null}',
                       '{"url": "https://example.org/secret=hidden"}',
                       '{"url": "https://rr1.googlevideo.com/secret=hidden", "http_headers": []}'):
            with self.subTest(output=output):
                good, detail = probe(done(0, output))
                self.assertIsNone(good)
                self.assertNotIn('secret=hidden', detail)

    def test_diagnostic_and_local_errors_are_unmeasured(self):
        for ready in (done(2), done(127), FileNotFoundError('secret=hidden'), PermissionError('secret=hidden'),
                      subprocess.TimeoutExpired('check', 35)):
            with self.subTest(ready=ready):
                good, detail = probe(ready=ready)
                self.assertIsNone(good)
                self.assertNotIn('secret=hidden', detail)
        for extraction in (KeyError('ytzdiag'), subprocess.TimeoutExpired('yt-dlp', 60)):
            with self.subTest(extraction=extraction):
                self.assertIsNone(probe(extraction=extraction)[0])

    def test_403_identifies_cdn_without_leaking_signed_media_url(self):
        good, detail = probe(download=done(22, {'http_code': 403, 'size_download': 0, 'speed_download': 0}))
        self.assertIsNone(good)
        self.assertIn('TCP video: HTTP 403', detail)
        self.assertIn('CDN=rr1.example.googlevideo.com', detail)
        self.assertNotIn('secret=hidden', detail)

    def test_only_video_network_evidence_is_failure(self):
        healthy = {'http_code': 206, 'size_download': 4194304, 'speed_download': 500000, 'content_type': 'video/mp4'}
        cases = [(0, healthy, True), (0, dict(healthy, speed_download=249999), False),
                 (0, dict(healthy, size_download=1000), False)]
        cases += [(code, dict(healthy, size_download=1000), False) for code in (18, 28, 52, 55, 56)]
        cases += [(code, {'http_code': 0}, False) for code in (7, 28, 52, 55, 56)]
        # Отказы сервиса/авторизации, неподдерживаемый Range и ошибки curl не доказывают поломку обхода.
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
                good, detail = probe(download=done(code, stats))
                self.assertIs(good, expected, detail)
                self.assertNotIn('secret=hidden', detail)
        for download in (FileNotFoundError('curl'), subprocess.TimeoutExpired('curl', 25), done(0, 'invalid')):
            with self.subTest(download=download):
                self.assertIsNone(probe(download=download)[0])


class LimitTests(unittest.TestCase):
    """Убитая по памяти или повисшая проба - не «YouTube недоступен»; сетевой провал не повторяется bgutil."""

    def assert_not_counted(self, ytdlp, curl=None, *, marker=None):
        calls = []
        state, changes, lines = tick(load(), fake_run(ytdlp, curl, calls), {'good': 0, 'bad': 2, 'pending': []})
        self.assertEqual(changes, [])
        self.assertEqual((state['bad'], state['good'], state['unmeasured']), (2, 0, True))
        self.assertEqual(len([args for args in calls if YTDLP in args]), 1, f'повторён заходом bgutil: {lines}')
        if marker == 'memory':
            own = [line for line in lines if 'проба не удалась: лимит памяти' in line]
            self.assertEqual(len(own), 1, lines)
            self.assertNotIn('good=', own[0])
        elif marker:
            self.assertIn(marker, state['last_probe'])

    def test_killed_extractor_or_curl_is_memory_limit(self):
        for rc in (-9, 137):
            with self.subTest(rc=rc):
                self.assert_not_counted(lambda args, rc=rc: subprocess.CompletedProcess(args, rc, '', ''),
                                        marker='memory')
        for stderr in ('ERROR: [youtube] aqz-KE-bpKQ: node exited with signal SIGKILL',
                       'FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory',
                       'MemoryError', 'OSError: [Errno 12] Cannot allocate memory'):
            with self.subTest(stderr=stderr):
                self.assert_not_counted(lambda args, stderr=stderr: subprocess.CompletedProcess(args, 1, '', stderr),
                                        marker='memory')
        self.assert_not_counted(lambda _args: done(0, {'url': URL}), lambda _args: done(-9), marker='memory')

    def test_timeout_of_first_attempt_skips_bgutil(self):
        def hang(args):
            raise subprocess.TimeoutExpired(args, 60)

        self.assert_not_counted(hang, marker='TimeoutExpired')
        self.assert_not_counted(lambda _args: done(0, {'url': URL}), hang, marker='TimeoutExpired')

    def test_network_failure_of_first_attempt_skips_bgutil(self):
        for stderr in ('Unable to download API page: <urlopen error [Errno 111] Connection refused>',
                       'Unable to download webpage: The read operation timed out',
                       'Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>',
                       'Unable to download webpage: <urlopen error [Errno -2] Name or service not known>',
                       'Unable to download webpage: <urlopen error [Errno 101] Network is unreachable>'):
            with self.subTest(stderr=stderr):
                calls = []
                state, changes, _ = tick(load(), fake_run(
                    lambda args, stderr=stderr: subprocess.CompletedProcess(args, 1, '', 'ERROR: ' + stderr), calls=calls))
                self.assertEqual((len([a for a in calls if YTDLP in a]), state['unmeasured'], changes), (1, True, []))
        calls = []
        state, _, _ = tick(load(), fake_run(lambda _args: done(0, {'url': URL}),
                                            lambda _args: done(6, {'http_code': 0}), calls))
        self.assertEqual((len([a for a in calls if YTDLP in a]), state['unmeasured']), (1, True))

    def test_token_or_403_failure_still_retries_with_bgutil(self):
        for stderr in ('ERROR: [youtube] aqz-KE-bpKQ: Sign in to confirm you’re not a bot',
                       'ERROR: unable to download video data: HTTP Error 403: Forbidden'):
            with self.subTest(stderr=stderr):
                calls = []

                def ytdlp(args, stderr=stderr):
                    if 'youtube:player_client=mweb;fetch_pot=always' in args:
                        return done(0, {'url': URL + '?pot=x'})
                    return subprocess.CompletedProcess(args, 1, '', stderr)

                state, _, _ = tick(load(), fake_run(ytdlp, calls=calls))
                self.assertEqual((len([a for a in calls if YTDLP in a]), state['unmeasured'], state['good']),
                                 (2, False, 1))

    def test_probe_unit_limit_follows_tier(self):
        def dropin(ram):
            out = resources.unit_dropin(ram, 'anysda-yt-failover')
            return dict(line.split('=', 1) for line in out.splitlines() if '=' in line)

        small = dropin(2048)
        self.assertEqual([small.get(key) for key in ('MemoryMax', 'Nice', 'CPUWeight', 'IOWeight')],
                         ['340M', '10', '20', '20'])
        for ram in (3072, 4096, 8192):
            self.assertGreaterEqual(int(dropin(ram)['MemoryMax'].rstrip('M')), 300, ram)


class PotTests(unittest.TestCase):
    """Отказ ролика на IP дата-центра не выключает видеопробу навсегда: локальный PO-токен."""

    def test_datacenter_refusal_uses_local_token_and_independent_video(self):
        watch = load()
        calls, caches = [], []
        user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())

        def run(args, **kwargs):
            calls.append(args)
            if args == CHECK:
                return subprocess.CompletedProcess(args, 0)
            self.assertEqual(args[:4], ['setpriv', f'--reuid={user.pw_uid}', f'--regid={user.pw_gid}', '--clear-groups'])
            if YTDLP in args:
                token = 'youtube:player_client=mweb;fetch_pot=always' in args
                if token:
                    self.assertNotIn('HTTPS_PROXY', kwargs['env'])
                    self.assertIn('youtubepot-bgutilscript:server_home=/opt/anysda-yt-probe/bgutil/server', args)
                    self.assertIn('--source-address', args)
                    self.assertIn(watch.POT_VIDEO, args)
                    self.assertNotEqual(watch.POT_VIDEO, watch.VIDEO)
                    cache = Path(kwargs['env']['XDG_CACHE_HOME'])
                    self.assertEqual((cache.is_dir(), cache.stat().st_uid), (True, user.pw_uid))
                    caches.append(cache)
                return done(0, {'url': 'https://first.googlevideo.com/videoplayback' + ('?pot=secret' if token else '')})
            token = '?pot=secret' in args[-1]
            if token:
                for flag in ('--location', '--max-redirs', '=https'):
                    self.assertIn(flag, args)
            return done(0 if token else 22, {'http_code': 206 if token else 403,
                                             'size_download': 4194304 if token else 0,
                                             'speed_download': 500000 if token else 0, 'content_type': 'video/mp4',
                                             'url_effective': 'https://last.googlevideo.com/video?pot=secret'})

        with patch.object(watch.subprocess, 'run', side_effect=run), \
                patch.object(watch.pwd, 'getpwnam', return_value=user), \
                patch.dict(os.environ, {'HTTPS_PROXY': 'http://proxy.invalid'}):
            good, detail = watch.probe()
        self.assertIs(good, True, detail)
        self.assertIn('HTTP 206, 4194304 байт', detail)
        self.assertIn('CDN=last.googlevideo.com, client=mweb, PO=local', detail)
        self.assertNotIn('secret', detail)
        self.assertEqual((len(calls), len(caches), caches[0].exists()), (5, 1, False))

    def test_measured_failure_or_success_is_never_retried_on_another_cdn(self):
        watch = load()
        for verdict in (False, True):
            with self.subTest(verdict=verdict), patch.object(watch.subprocess, 'run', return_value=done()), \
                    patch.object(watch.pwd, 'getpwnam'), \
                    patch.object(watch, 'probe_media', return_value=(verdict, 'measured')) as media:
                self.assertEqual(watch.probe(), (verdict, 'measured'))
                media.assert_called_once()

    def test_provider_failure_and_foreign_redirect_cannot_be_success(self):
        watch = load()
        user = SimpleNamespace(pw_uid=42, pw_gid=42)
        for token, target in ((False, 'https://cdn.googlevideo.com/v'), (True, 'https://example.org/video'),
                              (True, 'http://cdn.googlevideo.com/video'), (True, 42)):
            with self.subTest(token=token, target=target):
                responses = [done(0, {'url': 'https://cdn.googlevideo.com/v' + ('?pot=secret' if token else '')}),
                             done(0, dict(GOOD, url_effective=target))]
                with patch.object(watch.subprocess, 'run', side_effect=responses):
                    good, detail = watch.probe_media(user, watch.POT_VIDEO, 'mweb', cache='/cache')
                self.assertIsNone(good)
                self.assertNotIn('secret', detail)

    def test_sabr_refusal_is_explained_without_logging_extractor_secrets(self):
        watch = load()
        with patch.object(watch.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 1, '', 'Requested format is not available; secret=hidden')):
            good, detail = watch.probe_media(SimpleNamespace(pw_uid=42, pw_gid=42), watch.POT_VIDEO, 'mweb',
                                             cache='/cache')
        self.assertIsNone(good)
        self.assertIn('SABR/ограничения ролика', detail)
        self.assertNotIn('secret', detail)


class Stand:
    """Тики раз в STEP, каждый - свежий модуль; общее только state-файл."""

    def __init__(self, path, now=1_700_000_000):
        self.path, self.now, self.messages, self.sent_at, self.refusals = path, now, [], [], 0

    def notify(self, detail):
        self.messages.append(detail)
        self.sent_at.append(self.now)
        if self.refusals:
            self.refusals -= 1
            return False
        return True

    def tick(self, good, detail='проба'):
        watch = load()
        with patch.object(watch, 'api', return_value={'now': 'youtube-ru'}), \
                patch.object(watch, 'notify', side_effect=self.notify), \
                patch.object(watch.time, 'time', return_value=self.now), patch('sys.stdout'):
            watch.tick(self.path, lambda: (good, detail))
        self.now += STEP

    def gap(self, seconds, limit=None):
        """Неизмеренные пробы, пока провал не продлится seconds; алерта раньше limit нет."""
        start = self.now
        while self.now - start <= seconds:
            self.tick(None, 'антибот')
        if limit is not None:
            early = [m for m, at in zip(self.messages, self.sent_at)
                     if 'не измерено' in m and start <= at and at - start <= limit]
            assert not early, f'алерт раньше порога {limit} с: {early}'

    def alerts(self, marker):
        return [message for message in self.messages if marker in message]


class UnmeasuredAlertTests(unittest.TestCase):
    """Алерт «не измерено» только после непрерывного провала дольше порога, «возобновлено» - один раз."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.stand = Stand(Path(directory.name) / 'state.json')
        self.limit = getattr(yt, 'UNMEASURED_ALERT_AFTER', 3600)

    def test_flapping_unmeasured_for_two_hours_sends_nothing(self):
        for index in range(2 * 3600 // STEP):
            self.stand.tick(None if index % 2 else True, 'антибот' if index % 2 else 'быстро')
        self.assertEqual(self.stand.messages, [])

    def test_long_gap_alerts_once_resume_once_second_gap_again(self):
        self.stand.gap(self.limit + STEP, self.limit)
        self.stand.gap(2 * 3600)
        self.assertEqual(len(self.stand.messages), 1, self.stand.messages)
        self.stand.tick(True, 'быстро')
        self.stand.tick(True, 'быстро')
        self.assertEqual(len(self.stand.alerts('возобновлено')), 1, self.stand.messages)
        self.stand.gap(self.limit + STEP, self.limit)
        self.assertEqual(len(self.stand.alerts('не измерено')), 2, self.stand.messages)
        self.assertEqual(len(self.stand.messages), 3, self.stand.messages)

    def test_short_gap_sends_neither_alert_nor_resume(self):
        self.stand.tick(True, 'быстро')
        self.stand.gap(self.limit - 2 * STEP)
        self.stand.tick(True, 'быстро')
        self.stand.tick(True, 'быстро')
        self.assertEqual(self.stand.messages, [])

    def test_gap_start_survives_between_processes_in_state_file(self):
        self.stand.tick(True, 'быстро')
        start = self.stand.now
        self.stand.tick(None, 'антибот')
        self.stand.tick(None, 'антибот')
        self.assertIn(start, json.loads(self.stand.path.read_text()).values())
        self.stand.gap(self.limit)
        self.assertEqual(len(self.stand.alerts('не измерено')), 1, self.stand.messages)

    def test_undelivered_alert_is_delivered_once_on_next_tick(self):
        self.stand.refusals = 1
        start = self.stand.now
        while self.stand.now - start <= self.limit + STEP and not self.stand.messages:
            self.stand.tick(None, 'антибот')
        self.assertEqual(len(self.stand.messages), 1, 'алерт после порога не отправлен')
        self.assertGreater(self.stand.sent_at[0] - start, self.limit, 'алерт раньше порога')
        self.stand.tick(None, 'антибот')
        self.stand.tick(None, 'антибот')
        self.assertEqual(len(self.stand.messages), 2, self.stand.messages)
        self.assertEqual(self.stand.messages[0], self.stand.messages[1])
        self.assertEqual(json.loads(self.stand.path.read_text())['pending'], [])

    def test_alerts_on_edges_resume_counting_and_retry_delivery(self):
        watch = load()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps({'good': 0, 'bad': 1, 'pending': []}))
            now = [1_700_000_000]
            gap = [(None, 'антибот')] * (self.limit // 300 + 2) + [(None, 'нет JS')]
            with patch.object(watch, 'api', return_value={'now': 'youtube-ru'}) as api, \
                    patch.object(watch.time, 'time', side_effect=lambda: now[0]), \
                    patch.object(watch, 'notify', side_effect=lambda _: notify.call_count > 1) as notify:
                for good, detail in gap + [(False, 'таймаут'), (None, 'антибот'), (True, 'быстро')]:
                    watch.tick(path, lambda: (good, detail))
                    now[0] += 300
                messages = [call.args[0] for call in notify.call_args_list]
                self.assertEqual(len(messages), 3)  # «не измерено», его повтор, «возобновлено»
                self.assertEqual(messages[0], messages[1])
                self.assertIn('не измерено', messages[0])
                self.assertIn('возобновлено', messages[2])
                self.assertTrue(all(call.args == ('GET',) for call in api.call_args_list))
            state = json.loads(path.read_text())
            self.assertEqual((state['good'], state['bad'], state['pending']), (1, 0, []))

    def seed(self, text):
        self.stand.path.write_text('{"good": 1, "bad": 0, "pending": [], "last_probe": "антибот", '
                                   f'"checked_at": {self.stand.now - STEP}, {text}}}\n')

    def assert_alert_once_after_threshold(self):
        self.stand.gap(self.limit + STEP, self.limit)
        self.stand.gap(3600)
        self.assertEqual(self.stand.messages, self.stand.alerts('не измерено'))
        self.assertEqual(len(self.stand.messages), 1, self.stand.messages)

    def test_broken_gap_start_or_flag_restarts_threshold(self):
        future = self.stand.now + 30 * 86400
        seeds = [f'"unmeasured": true, "unmeasured_alerted": false, "unmeasured_since": {since}'
                 for since in ('NaN', '0', '-1', '-Infinity', 'Infinity', str(future), '"вчера"', 'true')]
        seeds += [f'"unmeasured": {flag}, "unmeasured_alerted": false, "unmeasured_since": {self.stand.now - 7200}'
                  for flag in ('false', 'null', '1')]
        for text in seeds:
            with self.subTest(text):
                self.setUp()
                self.seed(text)
                self.assert_alert_once_after_threshold()

    def test_previous_format_measured_state_stays_silent(self):
        self.seed('"unmeasured": false')
        self.stand.tick(True, 'быстро')
        self.stand.gap(self.limit - 2 * STEP)
        self.stand.tick(True, 'быстро')
        self.assertEqual(self.stand.messages, [])


class QuicPrefixTests(unittest.TestCase):
    """`vpn2 nft yt-quic`: список префиксов Google без дыр и расширений, все туннели."""

    def generate(self, prefixes):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'goog.json'
            path.write_text(json.dumps({'prefixes': prefixes}))
            return subprocess.run([sys.executable, '-m', 'vpn2.cli', 'nft', 'yt-quic', str(path)],
                                  capture_output=True, text=True, timeout=10,
                                  env=os.environ | {'PYTHONPATH': str(ROOT / 'infra/vpn2/src')})

    def test_rejects_incomplete_or_widened_list(self):
        v4, v6 = {'ipv4Prefix': '192.0.2.0/24'}, {'ipv6Prefix': '2001:db8::/32'}
        for prefixes in ([v4], [v6], [v4, v6, {}], [v4, v6, {'ipv4Prefix': '0.0.0.0/0'}],
                         [v4, v6, {'ipv4Prefix': '198.51.100.1/24'}],
                         [{'ipv4Prefix': '192.0.2.0/24', 'ipv6Prefix': '2001:db8::/32'}]):
            with self.subTest(prefixes=prefixes):
                result = self.generate(prefixes)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, '')

    def test_collapses_overlaps_and_covers_all_tunnels(self):
        result = self.generate([v for v in ({'ipv4Prefix': '192.0.2.0/24'}, {'ipv4Prefix': '192.0.2.0/25'},
                                            {'ipv6Prefix': '2001:db8::/32'})])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('192.0.2.0/25', result.stdout)
        for iface in ('wg0', 'tun0', 'xfrm0'):
            for family in ('ip daddr @google_v4', 'ip6 daddr @google_v6'):
                self.assertIn(f'iifname "{iface}" udp dport 443 {family} counter drop', result.stdout)


if __name__ == '__main__':
    unittest.main()
