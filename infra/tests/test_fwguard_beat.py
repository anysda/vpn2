"""Плеи пульса сторожа (deploy.yml, тег beat) на настоящем ansible: вход на entry с экзитов замечен."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from vpn_roles import ansible_playbook

ROOT = Path(__file__).resolve().parents[2]


class GuardBeatTests(unittest.TestCase):
    def beat(self, work, *extra):
        local = {'ansible_connection': 'local', 'ansible_host': '127.0.0.1'}
        inventory = {'entry': {'hosts': {'ru': local}}, 'exits': {'hosts': {'fi': local}}}
        (work / 'inventory.json').write_text(json.dumps(inventory))
        # Порт entry экзит уже проверил: плей экзитов не идёт (--limit ru), его итог - в entry_port.
        seen = json.dumps({'entry_port': {'rc': 0}})
        env = {**os.environ, 'ANSIBLE_CONFIG': str(ROOT / 'infra/ansible/ansible.cfg')}
        return subprocess.run([ansible_playbook(), '-i', str(work / 'inventory.json'), str(ROOT / 'infra/ansible/deploy.yml'),
                               '--tags', 'beat', '--limit', 'ru', '-e', seen, *extra],
                              env=env, capture_output=True, text=True, timeout=120)

    def test_exits_seeing_entry_is_recorded(self):
        with tempfile.TemporaryDirectory() as work:
            done = self.beat(Path(work), '-e', f'out={work}')
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertTrue((Path(work) / 'entry-outside').exists())

    def test_entry_pulse_runs_when_exits_see_entry(self):
        with tempfile.TemporaryDirectory() as work:
            done = self.beat(Path(work), '-e', 'entry_outside=True')
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertIn('ok: [ru]', done.stdout)


if __name__ == '__main__':
    unittest.main()
