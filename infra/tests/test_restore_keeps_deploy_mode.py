"""restore передаёт стадиям режим деплоя (VPN2_DEVELOPMENT).

deploy.sh restore кладёт значение перед запуском anysda-restore.sh на entry,
а блок STAGE_ENV в anysda-restore.sh пишет его в env стадий. Без этого стадия
30-frontend после деплоя --development идёт как установка по манифесту.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


def stage_env_block():
    text = (ROOT / 'infra/lib/anysda-restore.sh').read_text()
    match = re.search(r'^ENVS_DIR=.*?^fi$', text, re.S | re.M)
    assert match, 'блок STAGE_ENV не найден'
    return match.group(0)


class TestRestoreKeepsDeployMode(unittest.TestCase):
    def test_deploy_passes_mode_to_restore_command(self):
        text = (ROOT / 'infra/deploy.sh').read_text()
        lines = [line for line in text.splitlines() if 'anysda-restore.sh' in line and 'ssh_exec' in line]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('VPN2_DEVELOPMENT=${VPN2_DEVELOPMENT:-false} /usr/local/bin/anysda-restore.sh', lines[0])

    def run_block(self, mode):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ['rm', '-rf', str(directory)])
        envs = directory / 'infra/envs'
        envs.mkdir(parents=True)
        (envs / 'all.env').write_text('AGH_PORT=3000\n')
        (envs / 'ru.env').write_text('HOST=ru\n')
        work = directory / 'work'
        work.mkdir()
        environment = {k: v for k, v in os.environ.items() if k != 'VPN2_DEVELOPMENT'}
        if mode is not None:
            environment['VPN2_DEVELOPMENT'] = mode
        script = f'SOURCE_ROOT={directory}\nWORK={work}\n{stage_env_block()}\n'
        subprocess.run(['bash', '-c', script], env=environment, check=True, timeout=30)
        return (work / 'ru.env').read_text().splitlines()

    def test_stage_env_gets_development_true(self):
        lines = self.run_block('true')
        self.assertIn('VPN2_DEVELOPMENT=true', lines)
        self.assertIn('AGH_PORT=3000', lines)

    def test_stage_env_defaults_to_false(self):
        self.assertIn('VPN2_DEVELOPMENT=false', self.run_block(None))


if __name__ == '__main__':
    unittest.main()
