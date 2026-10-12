"""Run real stage fragments; configuration values must stay literal data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
VALUES = ['operator"name', "operator'name", r'operator\name',
          'operator$HOME$(touch INJECTED)', 'operator\nname\n']


def source(stage):
    return (ROOT / 'infra/scripts' / stage).read_text()


def run(script, directory, **values):
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                          cwd=directory, env=dict(os.environ, **values),
                          text=True, capture_output=True, timeout=15)


class AdguardLiteralTests(unittest.TestCase):
    def test_fresh_credentials(self):
        text = source('22-adguard.sh')
        block = text[text.index('agh_fresh=()'):text.index('\nelse\n  # Config exists')]
        for value in [*VALUES, 'true', 'name: value', '*alias']:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                path = Path(work) / 'fresh.yaml'
                result = run('svc_forget() { :; }\n' + block + '\nfi', work,
                             AGH_CFG=str(path), AGH_USER=value, AGH_PASS_HASH='test-hash',
                             AGH_PORT='3000', MGMT_IP='192.0.2.1',
                             AGH_ADAWAY='true', AGH_QUERYLOG='1000')
                self.assertEqual(result.returncode, 0, result.stderr)
                cfg = yaml.safe_load(path.read_text())
                self.assertEqual(cfg['users'], [{'name': value, 'password': 'test-hash'}])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sync_credentials(self):
        text = source('22-adguard.sh')
        block = text[text.index('  agh_sync=$('):text.index('\npatch_agh_config "$AGH_CFG"')]
        block = block.rsplit('\nfi', 1)[0]
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                path = Path(work) / 'config"\\$.yaml'
                path.write_text(yaml.safe_dump({'users': [], 'custom': 'keep'}))
                # Exercise config2env as well as source of its shell assignments.
                config = ('entry:\n  host: entry.example.test\n  password: fake\n'
                          'exits:\n  - tag: test\n    host: exit.example.test\n'
                          '    password: fake\nadmin:\n  user: ' + json.dumps(value) + '\n')
                (Path(work) / 'config.yaml').write_text(config)
                converted = subprocess.run([sys.executable, str(ROOT / 'infra/lib/config2env.py'), work],
                                           text=True, capture_output=True, timeout=15)
                self.assertEqual(converted.returncode, 0, converted.stderr)
                script = ('source infra/envs/ru.env\nAGH_USER=$ADMIN_USER\n'
                          'svc_forget() { echo restart; }\n' + block)
                first = run(script, work, AGH_CFG=str(path), AGH_PASS_HASH=value, HOST_TAG='ru')
                self.assertEqual(first.returncode, 0, first.stderr)
                cfg = yaml.safe_load(path.read_text())
                self.assertEqual(cfg['users'], [{'name': value, 'password': value}])
                self.assertEqual(cfg['custom'], 'keep')
                self.assertIn('restart', first.stdout)
                before = path.read_bytes()
                second = run(script, work, AGH_CFG=str(path), AGH_PASS_HASH=value, HOST_TAG='ru')
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertNotIn('restart', second.stdout)
                self.assertEqual(before, path.read_bytes())
                self.assertFalse((Path(work) / 'INJECTED').exists())


class TelegramLiteralTests(unittest.TestCase):
    def test_runtime_json(self):
        text = source('35-telegram.sh')
        start = text.index('if [[ ! -f /etc/anysda/telegram-runtime.json')
        block = text[start:text.index('\nfi', start) + 3]
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                path = Path(work) / 'telegram-runtime.json'
                script = block.replace('/etc/anysda/telegram-runtime.json', str(path))
                result = run(script, work, TELEGRAM_BOT_TOKEN=value, TELEGRAM_CHAT_ID=value,
                             TELEGRAM_ADMIN_USERNAME=value, HOST_TAG='ru')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(path.read_text()),
                                 dict(bot_token=value, chat_id=value, admin_username=value))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                second = run(script, work, HOST_TAG='ru')
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(json.loads(path.read_text())['bot_token'], value)
                self.assertFalse((Path(work) / 'INJECTED').exists())


if __name__ == '__main__':
    unittest.main()
