"""Entry reinstalled from backup while the exits keep running (docs/backup.md).

The exits leave the old installation by themselves (test_emergency_agent);
here: verify right after stage 10 meets an emergency the new entry ends.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
NOW = int(time.time()) - 10_000

STANDBY = '''import sys
from pathlib import Path
from vpn2.verify import stage as emergency

root = Path(sys.argv[1])
emergency.STAMPS = root
before_stage = not emergency.after_stage()
code, output = emergency.emergency_standby(root / 'hy2.json', root / 'state/agent.json', before_stage)
print(output)
raise SystemExit(code)
'''


class TestStandbyBeforeStage23(unittest.TestCase):
    """Verify right after 10-foreign meets an emergency the new entry ends."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'bin').mkdir()
        ss = self.root / 'bin/ss'
        ss.write_text('#!/bin/sh\necho "UNCONN 0 0 0.0.0.0:18443 0.0.0.0:*"\n')
        ss.chmod(0o755)
        (self.root / 'hy2.json').write_text(json.dumps({'server_ip': '192.0.2.2', 'port': 18443}))
        (self.root / 'state').mkdir()

    def standby(self, stage23_newer, active):
        (self.root / 'state/agent.json').write_text(json.dumps({'leader': {'active': active}}))
        stamps = [self.root / '10-foreign', self.root / '23-emergency']
        for index, path in enumerate(stamps if stage23_newer else stamps[::-1]):
            path.touch()
            os.utime(path, (NOW + index, NOW + index))
        env = dict(os.environ, PATH=f"{self.root / 'bin'}:{os.environ['PATH']}",
                   PYTHONPATH=str(ROOT / 'infra/vpn2/src'))
        return subprocess.run([sys.executable, '-B', '-c', STANDBY, str(self.root)], env=env,
                              capture_output=True, text=True)

    def test_listening_port_during_emergency_before_stage_23(self):
        self.assertEqual(self.standby(stage23_newer=False, active=True).returncode, 0)

    def test_listening_port_stays_a_failure_otherwise(self):
        for stage23_newer, active in ((True, True), (False, False), (True, False)):
            with self.subTest(stage23_newer=stage23_newer, active=active):
                result = self.standby(stage23_newer, active)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('udp/18443 уже слушает', result.stdout)


if __name__ == '__main__':
    unittest.main()
