"""Алерт «не измерено» только после непрерывного провала дольше порога."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_yt_failover import load_watch

STEP = 300  # период systemd-таймера


def threshold():
    return getattr(load_watch(), 'UNMEASURED_ALERT_AFTER', 3600)


class Stand:
    """Каждый tick - новый процесс: свежий модуль, общее только state-файл."""

    def __init__(self, path, now=1_700_000_000):
        self.path = path
        self.now = now
        self.messages = []
        self.sent_at = []
        self.refusals = 0

    def notify(self, detail):
        self.messages.append(detail)
        self.sent_at.append(self.now)
        if self.refusals:
            self.refusals -= 1
            return False
        return True

    def tick(self, good, detail='проба'):
        watch = load_watch()
        with patch.object(watch, 'api', return_value={'now': 'youtube-ru'}), \
                patch.object(watch, 'notify', side_effect=self.notify), \
                patch.object(watch.time, 'time', return_value=self.now), \
                patch('sys.stdout'):
            watch.tick(self.path, lambda: (good, detail))
        self.now += STEP

    def gap(self, seconds, limit=None):
        """Неизмеренные пробы каждые STEP, пока провал не продлится seconds."""
        start = self.now
        while self.now - start <= seconds:
            self.tick(None, 'антибот')
        if limit is not None:
            early = [message for message, at in zip(self.messages, self.sent_at)
                     if 'не измерено' in message and start <= at and at - start <= limit]
            assert not early, f'алерт раньше порога {limit} с: {early}'

    def alerts(self, marker):
        return [message for message in self.messages if marker in message]


class UnmeasuredAlertThresholdTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.stand = Stand(Path(directory.name) / 'state.json')
        self.limit = threshold()

    def test_flapping_unmeasured_every_check_for_two_hours_sends_nothing(self):
        for index in range(2 * 3600 // STEP):
            self.stand.tick(None if index % 2 else True, 'антибот' if index % 2 else 'быстро')
        self.assertEqual(self.stand.messages, [])

    def test_long_gap_alerts_once_and_stays_silent_while_still_unmeasured(self):
        self.stand.gap(self.limit + STEP, self.limit)
        self.assertEqual(len(self.stand.alerts('не измерено')), 1, self.stand.messages)
        self.stand.gap(2 * 3600)
        self.assertEqual(len(self.stand.alerts('не измерено')), 1, self.stand.messages)
        self.assertEqual(len(self.stand.messages), 1, self.stand.messages)

    def test_resume_after_long_gap_alerts_once(self):
        self.stand.gap(self.limit + STEP, self.limit)
        self.stand.tick(True, 'быстро')
        self.stand.tick(True, 'быстро')
        self.assertEqual(len(self.stand.alerts('возобновлено')), 1, self.stand.messages)
        self.assertEqual(len(self.stand.messages), 2, self.stand.messages)

    def test_short_gap_sends_neither_alert_nor_resume(self):
        self.stand.tick(True, 'быстро')
        self.stand.gap(self.limit - 2 * STEP)
        self.stand.tick(True, 'быстро')
        self.stand.tick(True, 'быстро')
        self.assertEqual(self.stand.messages, [])

    def test_second_long_gap_after_resume_alerts_again(self):
        self.stand.gap(self.limit + STEP, self.limit)
        self.stand.tick(True, 'быстро')
        self.stand.gap(self.limit + STEP, self.limit)
        self.assertEqual(len(self.stand.alerts('не измерено')), 2, self.stand.messages)
        self.assertEqual(len(self.stand.alerts('возобновлено')), 1, self.stand.messages)

    def test_gap_start_survives_between_processes_in_state_file(self):
        self.stand.tick(True, 'быстро')
        start = self.stand.now
        self.stand.tick(None, 'антибот')
        self.stand.tick(None, 'антибот')
        state = json.loads(self.stand.path.read_text())
        self.assertIn(start, state.values(), state)
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


if __name__ == '__main__':
    unittest.main()
