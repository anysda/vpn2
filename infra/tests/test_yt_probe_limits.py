"""Проба YouTube на 2 ГБ: потолок памяти, убийство по лимиту и лишний заход bgutil.

Убитая по памяти проба - не «YouTube недоступен»: счётчики и маршрут стоят,
в журнале отдельная строка. Сетевой провал первого захода не повторяется
вторым, тяжёлым заходом с PO-токеном; провал по токену/403 - повторяется.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_deploy import ROOT
from zapret_support import role_file
from test_yt_failover import load_watch

YTDLP = '/opt/anysda-yt-probe/bin/yt-dlp'
URL = 'https://rr1.example.googlevideo.com/videoplayback'
GOOD = {'http_code': 206, 'size_download': 4194304, 'speed_download': 500000,
        'content_type': 'video/mp4', 'url_effective': URL}


def fake_run(ytdlp, curl=None, calls=None):
    """subprocess.run пробы: ytdlp(аргументы) и curl(аргументы) дают CompletedProcess."""
    def run(args, **_):
        if calls is not None:
            calls.append(args)
        if args == ['/usr/local/bin/anysda-yt-check']:
            return subprocess.CompletedProcess(args, 0)
        if YTDLP in args:
            return ytdlp(args)
        return curl(args) if curl else subprocess.CompletedProcess(args, 0, json.dumps(GOOD))
    return run


def ytdlp_calls(calls):
    return [args for args in calls if YTDLP in args]


class ProbeLimitTests(unittest.TestCase):
    def tick(self, watch, run, state=None):
        """Один тик таймера: (state после, смены маршрута, строки журнала)."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            if state is not None:
                path.write_text(json.dumps(state))
            current = ['youtube-ru']
            changes = []

            def api(method, body=None):
                if method == 'PUT':
                    current[0] = body['name']
                    changes.append(body['name'])
                return {'now': current[0]}

            user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch.object(watch.subprocess, 'run', side_effect=run), \
                    patch.object(watch.pwd, 'getpwnam', return_value=user), \
                    patch.object(watch, 'api', side_effect=api), \
                    patch.object(watch, 'notify', return_value=True), \
                    patch('builtins.print') as printed:
                watch.tick(path, watch.probe)
            lines = [' '.join(str(arg) for arg in call.args) for call in printed.call_args_list]
            return json.loads(path.read_text()), changes, lines

    def assert_memory_limit(self, ytdlp, curl=None):
        watch = load_watch()
        calls = []
        # Две плохие пробы подряд уже были: третья, убитая по памяти, не должна
        # довести счётчик до переключения.
        state, changes, lines = self.tick(watch, fake_run(ytdlp, curl, calls),
                                          {'good': 0, 'bad': 2, 'pending': []})
        self.assertEqual(changes, [], 'проба, убитая по памяти, переключила маршрут')
        self.assertEqual(state['bad'], 2, 'убитая по памяти проба посчитана как «YouTube недоступен»')
        self.assertEqual(state['good'], 0)
        self.assertTrue(state['unmeasured'])
        own = [line for line in lines if 'проба не удалась: лимит памяти' in line]
        self.assertEqual(len(own), 1, f'нет отдельной строки журнала о лимите памяти: {lines}')
        self.assertNotIn('good=', own[0], 'строка о лимите слита с итоговой строкой тика')
        self.assertEqual(len(ytdlp_calls(calls)), 1,
                         'после убийства по памяти запущен второй, более тяжёлый заход bgutil')

    def test_sigkilled_extractor_is_memory_limit_not_youtube_down(self):
        for rc in (-9, 137):
            with self.subTest(rc=rc):
                self.assert_memory_limit(lambda args, rc=rc: subprocess.CompletedProcess(args, rc, '', ''))

    def test_killed_node_or_oom_message_is_memory_limit(self):
        for stderr in ('ERROR: [youtube] aqz-KE-bpKQ: node exited with signal SIGKILL',
                       'FATAL ERROR: Reached heap limit Allocation failed - '
                       'JavaScript heap out of memory',
                       'MemoryError', 'OSError: [Errno 12] Cannot allocate memory'):
            with self.subTest(stderr=stderr):
                self.assert_memory_limit(
                    lambda args, stderr=stderr: subprocess.CompletedProcess(args, 1, '', stderr))

    def test_sigkilled_curl_is_memory_limit(self):
        self.assert_memory_limit(
            lambda args: subprocess.CompletedProcess(args, 0, json.dumps({'url': URL})),
            lambda args: subprocess.CompletedProcess(args, -9, '', ''))

    def test_network_failure_of_first_attempt_skips_bgutil(self):
        for stderr in ('ERROR: [youtube] aqz-KE-bpKQ: Unable to download API page: '
                       '<urlopen error [Errno 111] Connection refused>',
                       'ERROR: [youtube] aqz-KE-bpKQ: Unable to download webpage: '
                       'The read operation timed out',
                       'ERROR: [youtube] aqz-KE-bpKQ: Unable to download webpage: <urlopen error '
                       '[Errno -3] Temporary failure in name resolution>',
                       'ERROR: Unable to download webpage: <urlopen error [Errno -2] '
                       'Name or service not known>',
                       'ERROR: Unable to download webpage: <urlopen error [Errno 101] '
                       'Network is unreachable>'):
            with self.subTest(stderr=stderr):
                watch = load_watch()
                calls = []
                state, changes, _ = self.tick(watch, fake_run(
                    lambda args, stderr=stderr: subprocess.CompletedProcess(args, 1, '', stderr),
                    calls=calls))
                self.assertEqual(len(ytdlp_calls(calls)), 1, 'сетевой провал повторён заходом bgutil')
                self.assertTrue(state['unmeasured'])
                self.assertEqual(changes, [])

    def test_dns_failure_of_googlevideo_skips_bgutil(self):
        watch = load_watch()
        calls = []
        state, _, _ = self.tick(watch, fake_run(
            lambda args: subprocess.CompletedProcess(args, 0, json.dumps({'url': URL})),
            lambda args: subprocess.CompletedProcess(args, 6, json.dumps({'http_code': 0})),
            calls))
        self.assertEqual(len(ytdlp_calls(calls)), 1, 'DNS Googlevideo повторён заходом bgutil')
        self.assertTrue(state['unmeasured'])

    def test_token_or_403_failure_still_retries_with_bgutil(self):
        # Положительный контроль: отказ по токену - ровно тот случай, ради
        # которого есть второй заход.
        for stderr in ('ERROR: [youtube] aqz-KE-bpKQ: Sign in to confirm you’re not a bot',
                       'ERROR: unable to download video data: HTTP Error 403: Forbidden'):
            with self.subTest(stderr=stderr):
                watch = load_watch()
                calls = []

                def ytdlp(args, stderr=stderr):
                    if 'youtube:player_client=mweb;fetch_pot=always' in args:
                        return subprocess.CompletedProcess(args, 0, json.dumps({'url': URL + '?pot=x'}))
                    return subprocess.CompletedProcess(args, 1, '', stderr)

                state, _, _ = self.tick(watch, fake_run(ytdlp, calls=calls))
                self.assertEqual(len(ytdlp_calls(calls)), 2)
                self.assertFalse(state['unmeasured'])
                self.assertEqual(state['good'], 1)

    def test_probe_unit_limit_follows_tier_and_survives_oom_kill(self):
        tool = ROOT / 'infra/lib/entry_resources.py'

        def dropin(ram):
            out = subprocess.check_output([sys.executable, str(tool), '--ram-mib', str(ram),
                                           '--free-mib', '8192', '--unit', 'anysda-yt-failover'],
                                          text=True)
            return dict(line.split('=', 1) for line in out.splitlines() if '=' in line)

        small = dropin(2048)
        self.assertEqual(small.get('MemoryMax'), '340M')
        for key, value in (('Nice', '10'), ('CPUWeight', '20'), ('IOWeight', '20')):
            self.assertEqual(small.get(key), value, key)
        for ram in (3072, 4096, 8192):
            with self.subTest(ram=ram):
                self.assertGreaterEqual(int(dropin(ram)['MemoryMax'].rstrip('M')), 300)
        # Без OOMPolicy=continue systemd гасит всю службу при первом OOM-kill
        # ребёнка, и контроллер не успевает записать строку о лимите.
        unit = role_file('anysda-yt-failover.service')
        self.assertIn('OOMPolicy=continue', unit.splitlines())


if __name__ == '__main__':
    unittest.main()
