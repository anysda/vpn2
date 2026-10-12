"""Поломки обхода и сбросы Googlevideo должны включать запасной маршрут."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_yt_check
from test_yt_failover import load_watch


class BypassFailureTests(unittest.TestCase):
    def assert_failover(self, watch):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            current = ['youtube-ru']
            changes = []

            def api(method, body=None):
                if method == 'PUT':
                    current[0] = body['name']
                    changes.append(current[0])
                return {'now': current[0]}

            with patch.object(watch, 'api', side_effect=api), \
                    patch.object(watch, 'notify', return_value=True), patch('builtins.print'):
                for hit in range(1, 4):
                    watch.tick(path, watch.probe)
                    state = json.loads(path.read_text())
                    self.assertEqual(state['bad'], hit)
                    self.assertFalse(state['unmeasured'])
                    self.assertEqual(changes, ['foreign-best'] if hit == 3 else [])

    def test_stopped_service_missing_queue_or_rules_switch_after_three_failures(self):
        for scenario in ('inactive', 'no_queue', 'no_table', 'no_rules', 'no_packets'):
            with self.subTest(scenario=scenario):
                # Исполняем реальный сгенерированный check с подставными systemctl/nft.
                ready = test_yt_check.YoutubeCheckTests().run_check(scenario)
                self.assertEqual(ready.returncode, 1, ready.stderr + ready.stdout)
                watch = load_watch()
                with patch.object(watch.subprocess, 'run', return_value=ready) as run:
                    self.assert_failover(watch)
                    self.assertEqual(run.call_count, 3)
                    self.assertTrue(all(call.args[0] == ['/usr/local/bin/anysda-yt-check']
                                        for call in run.call_args_list))

    def test_googlevideo_handshake_reset_and_truncated_transfer_switch(self):
        cases = [(rc, {'http_code': 0}) for rc in (7, 28, 35, 52, 55, 56)]
        cases += [(rc, {'http_code': 206, 'content_type': 'video/mp4',
                       'size_download': 16384, 'speed_download': 819})
                  for rc in (0, 18, 28, 55, 56)]
        for rc, stats in cases:
            with self.subTest(rc=rc, stats=stats):
                watch = load_watch()
                responses = [subprocess.CompletedProcess([], 0),
                             subprocess.CompletedProcess([], 0, json.dumps({
                                 'url': 'https://rr1.example.googlevideo.com/videoplayback'})),
                             subprocess.CompletedProcess([], rc, json.dumps(stats))]
                with patch.object(watch.subprocess, 'run', side_effect=responses * 3), \
                        patch.object(watch.pwd, 'getpwnam'):
                    self.assert_failover(watch)

    def test_missing_diagnostic_tools_are_unmeasured(self):
        for scenario in ('missing_curl', 'missing_nft', 'missing_python3', 'bad_setpriv',
                         'dns', 'certificate', 'http_refusal'):
            with self.subTest(scenario=scenario):
                ready = test_yt_check.YoutubeCheckTests().run_check(scenario)
                self.assertEqual(ready.returncode, 2, ready.stderr + ready.stdout)
                watch = load_watch()
                with patch.object(watch.subprocess, 'run', return_value=ready):
                    self.assertIsNone(watch.probe()[0])


if __name__ == '__main__':
    unittest.main()
