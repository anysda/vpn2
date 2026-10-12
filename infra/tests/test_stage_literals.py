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


if __name__ == '__main__':
    unittest.main()
