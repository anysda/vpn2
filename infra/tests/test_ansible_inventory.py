import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / 'infra'
TEMPLATE = INFRA / 'tests/fixtures/config/etalon.yaml'
FIXTURE = INFRA / 'tests/fixtures/ansible/inventory-etalon.json'
LABELS = {'@ENTRY_HOST@': '198.18.10.10', '@EXIT_HOST@': '198.18.10.20', '@EXIT_TAG@': 'nl',
          '@PASSWORD@': 'stand-pass', '@PANEL_IMAGE@': 'panel:test'}


def render(extra_exits=''):
    text = TEMPLATE.read_text(encoding='utf-8')
    for label, value in LABELS.items():
        text = text.replace(label, value)
    return text if not extra_exits else text.replace('admin:\n', extra_exits + 'admin:\n', 1)


def run(directory, text, *args):
    path = Path(directory) / 'config.yaml'
    path.write_text(text, encoding='utf-8')
    # Окружение пустое: настройки запуска из окружения инвентарь отдаёт узлам, эталону они не нужны.
    env = dict(PATH=os.environ['PATH'], VPN2_CONFIG=str(path), PYTHONPATH=str(INFRA / 'vpn2/src'),
               VPN2_STATE_DIR=str(Path(directory) / 'state'))
    return subprocess.run([sys.executable, 'infra/ansible/inventory.py', *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=30)


class AnsibleInventoryTests(unittest.TestCase):
    def test_inventory_is_executable_in_git(self):
        # Без бита исполнения ansible читает inventory.py как ini и узлов не видит.
        mode = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-s', 'infra/ansible/inventory.py'],
                              text=True, capture_output=True, check=True).stdout.split()[:1]
        if not mode:
            self.skipTest('дерево не из git')
        self.assertEqual(mode, ['100755'])

    def test_list_matches_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run(directory, render(), '--list')
            state = str(Path(directory) / 'state')
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout.replace(state, '@STATE@'))
        self.assertEqual(actual, json.loads(FIXTURE.read_text(encoding='utf-8')))

    def test_host_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run(directory, render(), '--host', 'ru')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})

    def test_exits_follow_config(self):
        second = '  - tag: de\n    host: 198.18.10.30\n    password: "stand-pass"\n'
        base = render()
        no_exits = base.split('exits:\n')[0] + 'admin:' + base.split('admin:', 1)[1]
        cases = ((no_exits, []), (render(second), ['nl', 'de']))
        for text, tags in cases:
            with self.subTest(tags=tags), tempfile.TemporaryDirectory() as directory:
                result = run(directory, text, '--list')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['exits']['hosts'], tags)


if __name__ == '__main__':
    unittest.main()
