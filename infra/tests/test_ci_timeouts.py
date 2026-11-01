"""VPN2-626: a hung step in checks.yml is cut off in minutes, not after GitHub's 360.

Attempt 1 of run 41254380077 sat silent in the panel-e2e QR step for 25 minutes
until it was cancelled by hand: no job or step in checks.yml had timeout-minutes.

VPN2_TEST_ROOT selects the unchanged baseline.
"""
import os
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
WORKFLOW = ROOT / '.github/workflows/checks.yml'

# Longest successful job in the last 12 green checks runs, minutes.
USUAL_MINUTES = {
    'shell': 1,
    'python-lint': 1,
    'infra': 26,
    'telegram': 4,
    'python': 1,
    'backup-restore': 8,
    'panel': 1,
    'backup-keys': 1,
    'panel-e2e': 18,
}
JOB_CEILING = 60
APT_STEP_CEILING = 10
APT_COMMAND_CEILING = 600
QR_SCRIPT = 'e2e/qr-size.sh'


def apt_lines(script):
    return [line.strip() for line in script.splitlines() if re.search(r'\bapt-get\b', line)]


def bounded_apt(line):
    match = re.match(r'(?:sudo\s+)?timeout\s+(?:-\S+\s+)*(\d+)s?\s+(?:sudo\s+)?apt-get\b', line)
    return bool(match) and int(match.group(1)) <= APT_COMMAND_CEILING


class CheckTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.jobs = yaml.safe_load(WORKFLOW.read_text())['jobs']

    def test_every_job_has_a_ceiling_with_headroom(self):
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                limit = job.get('timeout-minutes')
                self.assertIsInstance(limit, int, f'{name}: no timeout-minutes, a hang holds the runner 360 min')
                self.assertLessEqual(limit, JOB_CEILING)
                usual = USUAL_MINUTES.get(name)
                if usual is not None:
                    self.assertGreaterEqual(limit, usual * 1.5, f'{name}: ceiling would cut green runs')

    def test_qr_step_has_own_ceiling_and_keeps_output(self):
        steps = [s for s in self.jobs['panel-e2e']['steps'] if QR_SCRIPT in s.get('run', '')]
        self.assertEqual(len(steps), 1)
        step = steps[0]
        limit = step.get('timeout-minutes')
        self.assertIsInstance(limit, int, 'QR step: no timeout-minutes')
        self.assertLessEqual(limit, 15)
        line = next(x for x in step['run'].splitlines() if QR_SCRIPT in x)
        self.assertNotRegex(line, r'>|\|', 'qr-size.sh output must reach the log')

    def test_apt_cannot_hang_silently_for_long(self):
        found = 0
        for name, job in self.jobs.items():
            for step in job.get('steps', []):
                lines = apt_lines(step.get('run', ''))
                if not lines:
                    continue
                found += len(lines)
                limit = step.get('timeout-minutes')
                if isinstance(limit, int) and limit <= APT_STEP_CEILING:
                    continue
                for line in lines:
                    with self.subTest(job=name, step=step.get('name'), line=line):
                        self.assertTrue(bounded_apt(line),
                                        f'apt-get without timeout in a step without timeout-minutes <= {APT_STEP_CEILING}')
        self.assertGreater(found, 0)


if __name__ == '__main__':
    unittest.main()
