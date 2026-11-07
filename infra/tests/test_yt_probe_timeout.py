"""Проба YouTube: внешний таймаут первого захода не запускает второй заход bgutil.

Повисший yt-dlp или curl - сетевой отказ до YouTube: второй, тяжёлый заход с
PO-токеном на том же пути повиснет так же и растянет пробу до TimeoutStartSec.
"""
import json
import subprocess
import unittest

import test_yt_probe_limits as limits
from test_yt_probe_limits import URL, fake_run, ytdlp_calls
from test_yt_failover import load_watch


def hang(args):
    raise subprocess.TimeoutExpired(args, 60)


class ProbeTimeoutTests(unittest.TestCase):
    tick = limits.ProbeLimitTests.tick

    def assert_single_attempt(self, ytdlp, curl=None):
        watch = load_watch()
        calls = []
        state, changes, lines = self.tick(watch, fake_run(ytdlp, curl, calls),
                                          {'good': 0, 'bad': 2, 'pending': []})
        self.assertEqual(len(ytdlp_calls(calls)), 1,
                         f'таймаут первого захода повторён заходом bgutil: {lines}')
        self.assertTrue(state['unmeasured'])
        self.assertEqual(state['bad'], 2, 'таймаут посчитан как «YouTube недоступен»')
        self.assertEqual(changes, [])
        self.assertIn('TimeoutExpired', state['last_probe'])

    def test_ytdlp_timeout_skips_bgutil(self):
        self.assert_single_attempt(hang)

    def test_curl_timeout_skips_bgutil(self):
        self.assert_single_attempt(
            lambda args: subprocess.CompletedProcess(args, 0, json.dumps({'url': URL})), hang)


if __name__ == '__main__':
    unittest.main()
