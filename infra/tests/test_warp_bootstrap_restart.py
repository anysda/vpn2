"""Deployment check of exit WARP restarts sing-box after the first failed probe."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
CONFIG = {
    'endpoints': [{'type': 'wireguard', 'tag': 'warp', 'peers': [{'address': '162.159.192.1'}]}],
    'experimental': {'clash_api': {'external_controller': '127.0.0.1:9090', 'secret': 'test-only'}},
}


class TestWarpBootstrapRestart(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('warp_watchdog', ROOT / 'infra/lib/warp-watchdog.py')
        cls.wd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.wd)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / 'state.json'

    def bootstrap(self, probes, start=1000):
        """Run --bootstrap on a fake clock; return the clock value of each restart."""
        clock = [start]
        restarts = []

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(self.wd, 'probe', side_effect=probes), \
                patch.object(self.wd.time, 'time', side_effect=lambda: clock[0]), \
                patch.object(self.wd.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.wd.time, 'sleep', side_effect=sleep), \
                patch.object(self.wd.subprocess, 'run', side_effect=lambda *_args, **_kwargs: restarts.append(clock[0])), \
                patch.object(self.wd, 'log'):
            self.wd.run(CONFIG, self.state, bootstrap=True)
        return restarts

    def test_first_failed_probe_restarts_at_once(self):
        restarts = self.bootstrap([False, True])
        self.assertEqual(restarts, [1000])

    def test_live_warp_is_not_restarted(self):
        self.assertEqual(self.bootstrap([True]), [])

    def test_failed_restart_keeps_backoff_for_second(self):
        restarts = self.bootstrap(iter(lambda: False, True))
        self.assertEqual(len(restarts), 2)
        self.assertEqual(restarts[1] - restarts[0], self.wd.BACKOFF[0])

    def test_timer_backoff_is_honoured(self):
        # The timer restarted 100 s ago: its backoff holds, WARP revives by itself.
        self.state.write_text(json.dumps({'restarts': [900], 'next_restart': 1200, 'level': 1, 'bad_since': 900}))
        restarts = self.bootstrap([False] * 9 + [True])
        self.assertEqual(restarts, [])

    def test_hourly_cap_is_not_exceeded(self):
        self.state.write_text(json.dumps({'restarts': [100, 400], 'next_restart': 900, 'level': 2}))
        restarts = self.bootstrap(iter(lambda: False, True))
        self.assertEqual(restarts, [1000])
        self.assertLessEqual(len(json.loads(self.state.read_text())['restarts']), self.wd.MAX_PER_HOUR)

    def test_timer_still_waits_dead_after(self):
        restarts = []
        with patch.object(self.wd, 'probe', return_value=False), \
                patch.object(self.wd.subprocess, 'run', side_effect=lambda *_args, **_kwargs: restarts.append(now)), \
                patch.object(self.wd, 'log'):
            for now in range(0, 1200, 30):
                with patch.object(self.wd.time, 'time', return_value=now):
                    self.wd.run(CONFIG, self.state)
        dead = self.wd.DEAD_AFTER
        self.assertEqual(restarts, [dead, dead + 300, dead + 900])


if __name__ == '__main__':
    unittest.main()
