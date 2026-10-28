"""Испорченное время провала и state прежнего формата не ломают алерт «не измерено»."""
import json
from pathlib import Path
import tempfile
import unittest

from test_yt_unmeasured_alerts import STEP, Stand, threshold


class UnmeasuredStateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.stand = Stand(Path(directory.name) / 'state.json')
        self.limit = threshold()

    def seed(self, text):
        self.stand.path.write_text(
            '{"good": 1, "bad": 0, "pending": [], "last_probe": "антибот", '
            f'"checked_at": {self.stand.now - STEP}, {text}}}\n')

    def assert_alert_once_after_threshold(self):
        self.stand.gap(self.limit + STEP, self.limit)
        self.assertEqual(len(self.stand.alerts('не измерено')), 1, self.stand.messages)
        self.stand.gap(3600)
        self.assertEqual(self.stand.messages, self.stand.alerts('не измерено'))
        self.assertEqual(len(self.stand.messages), 1, self.stand.messages)

    def test_broken_gap_start_restarts_threshold(self):
        future = self.stand.now + 30 * 86400
        for since in ('NaN', '0', '-1', '-Infinity', 'Infinity', str(future), '"вчера"', 'true'):
            with self.subTest(since=since):
                self.setUp()
                self.seed('"unmeasured": true, "unmeasured_alerted": false, '
                          f'"unmeasured_since": {since}')
                self.assert_alert_once_after_threshold()

    def test_gap_start_without_unmeasured_flag_is_ignored(self):
        for flag in ('false', 'null', '1'):
            with self.subTest(unmeasured=flag):
                self.setUp()
                self.seed(f'"unmeasured": {flag}, "unmeasured_alerted": false, '
                          f'"unmeasured_since": {self.stand.now - 2 * 3600}')
                self.assert_alert_once_after_threshold()

    def test_previous_format_mid_gap_resume_alerts_once(self):
        self.seed('"unmeasured": true')
        self.stand.gap(self.limit // 2)
        self.stand.tick(True, 'быстро')
        self.stand.tick(True, 'быстро')
        self.assertEqual(len(self.stand.alerts('возобновлено')), 1, self.stand.messages)
        self.assertEqual(len(self.stand.messages), 1, self.stand.messages)

    def test_previous_format_mid_gap_continues_silently(self):
        self.seed('"unmeasured": true')
        self.stand.gap(2 * 3600)
        self.assertEqual(self.stand.messages, [])
        state = json.loads(self.stand.path.read_text())
        self.assertIs(state['unmeasured_alerted'], True, state)

    def test_previous_format_measured_state_stays_silent(self):
        self.seed('"unmeasured": false')
        self.stand.tick(True, 'быстро')
        self.stand.gap(self.limit - 2 * STEP)
        self.stand.tick(True, 'быстро')
        self.assertEqual(self.stand.messages, [])


if __name__ == '__main__':
    unittest.main()
