"""Проверки пользовательского ввода до записи env и выполнения shell."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT


CONFIG = """entry:
  host: 192.0.2.1
  password: 'entry secret'
exits:
  - tag: se
    host: 192.0.2.2
    password: 'exit secret'
"""


class ConfigValidationTests(unittest.TestCase):
    def convert(self, config, directory):
        (Path(directory) / 'config.yaml').write_text(config)
        return subprocess.run(['python3', str(ROOT / 'infra/lib/config2env.py'), directory],
                              capture_output=True, text=True, timeout=10,
                              env={**os.environ, 'HOME': directory})

    def test_unsafe_env_fields_rejected_before_writes(self):
        cases = [
            (CONFIG + 'ports:\n  hy2_direct: "443; touch CANARY"\n', 'ports.hy2_direct'),
            (CONFIG.replace('host: 192.0.2.2', 'host: "$(touch CANARY)"'), 'exits[0].host'),
            (CONFIG.replace('tag: se', 'tag: "$(touch CANARY)"'), 'exits[0].tag'),
            (CONFIG.replace('tag: se', 'tag: ../escaped'), 'exits[0].tag'),
        ]
        for config, key in cases:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                result = self.convert(config, directory)
                envs = Path(directory) / 'infra/envs'
                if (envs / 'all.env').exists():
                    subprocess.run(['bash', '-c', 'source infra/envs/all.env'],
                                   cwd=directory, capture_output=True, timeout=5)
                self.assertFalse((Path(directory) / 'CANARY').exists(), 'shell injection')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)
                self.assertFalse(envs.exists())

    def test_ports_and_tags(self):
        cases = []
        for value in ('0', '65536', '-1', '1.5', 'true', "''"):
            cases.append((CONFIG + f'ports:\n  hy2_warp: {value}\n', 'ports.hy2_warp'))
            cases.append((CONFIG + f'    hy2_direct_port: {value}\n', 'exits[0].hy2_direct_port'))
        for tag in ('ru', 'all', 'exits', 'foreign', 'SE'):
            cases.append((CONFIG.replace('tag: se', f'tag: {tag}'), 'exits[0].tag'))
        cases.append((CONFIG + '  - tag: se\n    host: 192.0.2.3\n    password: x\n', 'exits[1].tag'))
        cases.append((CONFIG.split('exits:')[0] + 'exits:\n', 'exits'))
        for config, key in cases:
            with self.subTest(key=key, config=config), tempfile.TemporaryDirectory() as directory:
                result = self.convert(config, directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_exit_tag_syntax(self):
        for tag in ('a', 'us2', 'us-2', 'us--2', '-us', 'us-', 'US', 'us_2'):
            valid = tag in ('a', 'us2', 'us-2', 'us--2')
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as directory:
                result = self.convert(CONFIG.replace('tag: se', f'tag: {tag}'), directory)
                self.assertEqual(result.returncode == 0, valid, result.stderr)
                envs = Path(directory) / 'infra/envs'
                if valid:
                    self.assertTrue((envs / f'{tag}.env').is_file())
                else:
                    self.assertIn('exits[0].tag', result.stderr)
                    self.assertFalse(envs.exists())

    def test_boundary_ports_ipv6_and_shell_quoted_secrets(self):
        secret = " spaces ' \" $HOME $(touch CANARY)\\nsecond line "
        config = CONFIG.replace("'entry secret'", '"' + secret.replace('"', '\\"') + '"')
        config = config.replace('192.0.2.2', '2001:db8::2')
        config += 'ports:\n  hy2_direct: 1\n  hy2_warp: 65535\n'
        with tempfile.TemporaryDirectory() as directory:
            result = self.convert(config, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(['bash', '-euc',
                                     'source infra/envs/all.env; source infra/envs/ru.env; '
                                     'printf "%s" "$SSH_PASS"'],
                                    cwd=directory, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, secret.replace('\\n', '\n'))
            self.assertFalse((Path(directory) / 'CANARY').exists())


if __name__ == '__main__':
    unittest.main()
