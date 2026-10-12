"""Exercise the actual stage readiness/stamp code with failed scrape targets."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class MonitoringHealthTest(unittest.TestCase):
    def run_health(self, targets):
        source = (Path(__file__).resolve().parents[1] / 'scripts/25-monitoring.sh').read_text()
        health = source[source.index('# 4. Проверка targets'):]
        with tempfile.TemporaryDirectory() as scratch:
            result = subprocess.run(
                ['bash', '-c', '''set -euo pipefail
curl() { printf '%s' "$RESPONSE"; }
sleep() { :; }
''' + health],
                env={**os.environ, 'HOST_TAG': 'ru', 'STAGE': '25-monitoring',
                     'STAMP_DIR': scratch, 'EXIT_TAGS': 'test',
                     'RESPONSE': json.dumps({'status': 'success', 'data': {'activeTargets': targets}})},
                capture_output=True, text=True, check=False,
            )
            return result.returncode, (Path(scratch) / '25-monitoring').exists()

    def test_failed_or_missing_scrape_does_not_stamp_success(self):
        for targets in ([], [{'health': 'down'}], [{'health': 'up'}, {'health': 'down'}]):
            with self.subTest(targets=targets):
                code, stamped = self.run_health(targets)
                self.assertNotEqual(code, 0)
                self.assertFalse(stamped)

    def test_all_targets_up_stamp_success(self):
        target = {'health': 'up', 'labels': {'host': 'ru'}, 'scrapeUrl': 'http://localhost/metrics'}
        self.assertEqual(self.run_health([target, target]), (0, True))


if __name__ == '__main__':
    unittest.main()
