"""Регрессии развёртывания: реальные фрагменты стадий с подставными командами.

VPN2_TEST_ROOT позволяет прогнать те же проверки против другой ревизии.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


def source(path):
    return (ROOT / path).read_text()


def function(name):
    body = source('infra/deploy.sh').split(f'{name}() {{', 1)[1].split('\n}', 1)[0]
    return f'{name}() {{' + body + '\n}\n'


def bash(script, directory):
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                          cwd=directory, text=True, capture_output=True, timeout=15)


class DeployTests(unittest.TestCase):
    def test_failed_upload_never_runs_stale_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'scripts/30-frontend.sh').touch()
            (root / 'envs').mkdir()
            (root / 'envs/all.env').write_text('COMMON=1\n')
            (root / 'envs/ru.env').write_text('HOST_TAG=ru\nSSH_PASS=fake\n')
            script = function('run_stage_on_host') + '''
DEPLOY_ROOT=$PWD
load_env() { return 0; }
log() { :; }
ok() { :; }
warn() { :; }
die() { exit 1; }
count=0
push() { count=$((count + 1)); [[ $count != $FAIL_AT ]]; }
ssh_exec() {
  if [[ $* == *apt_prepare* ]]; then [[ $FAIL_AT != apt ]]; return; fi
  echo "REMOTE EXECUTED"
}
if run_stage_on_host 30-frontend ru; then exit 0; else exit 23; fi
'''
            for failure in ('1', '2', '3', '4', 'apt'):
                with self.subTest(failure=failure):
                    result = bash(f'FAIL_AT={failure}\n' + script, directory)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertNotIn('REMOTE EXECUTED', result.stdout)
            result = bash('FAIL_AT=none\n' + script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count('REMOTE EXECUTED'), 1)


if __name__ == '__main__':
    unittest.main()
