"""Вход ansible на узел (Mitogen, ansible.cfg): подключение повторяется, ключ хоста - как accept-new."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from vpn_roles import ansible_playbook

ROOT = Path(__file__).resolve().parents[2]
# Подставной ssh пишет свои аргументы и ведёт себя как ssh перед узлом с незнакомым или сменившимся ключом.
FAKE_SSH = """#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
if [ "$FAKE_MODE" = unknown ]; then
  printf 'Are you sure you want to continue connecting (yes/no/[fingerprint])? ' > /dev/tty
  read -r answer < /dev/tty; echo "answer=$answer" >> "$FAKE_LOG"
else
  echo 'Host key verification failed.' >&2
fi
exit 255
"""


class SshLoginTests(unittest.TestCase):
    def login(self, mode):
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            (work / 'ssh').write_text(FAKE_SSH)
            (work / 'ssh').chmod(0o755)
            node = {'ansible_host': '198.18.0.1', 'ansible_user': 'root', 'ansible_ssh_executable': f'{work}/ssh',
                    'ansible_ssh_common_args': '-o ConnectTimeout=10'}
            (work / 'inventory.yml').write_text(json.dumps({'all': {'hosts': {'node': node}}}))
            (work / 'play.yml').write_text('- hosts: node\n  gather_facts: false\n  tasks: [ansible.builtin.ping: {}]\n')
            env = {**os.environ, 'ANSIBLE_CONFIG': str(ROOT / 'infra/ansible/ansible.cfg'),
                   'FAKE_MODE': mode, 'FAKE_LOG': str(work / 'log')}
            started = time.monotonic()
            done = subprocess.run([ansible_playbook(), '-i', str(work / 'inventory.yml'), str(work / 'play.yml')],
                                  env=env, capture_output=True, text=True, timeout=120)
            return done, (work / 'log').read_text(), time.monotonic() - started

    def test_connect_is_retried_long_enough_for_a_40s_path_loss(self):
        done, log, _ = self.login('unknown')
        args = log.splitlines()[0]
        self.assertEqual(done.returncode, 4, done.stdout)
        # 9 попыток по 10 с: обрыв пути на 40 с переживается, срок входа Mitogen их дожидается.
        self.assertIn('-o ConnectionAttempts=9 -o ConnectTimeout=10', args)
        cfg = (ROOT / 'infra/ansible/ansible.cfg').read_text()
        self.assertIn('\ntimeout = 90\n', cfg)

    def test_unknown_host_key_is_accepted(self):
        _, log, _ = self.login('unknown')
        self.assertIn('-o StrictHostKeyChecking ask', log)
        self.assertIn('answer=yes', log)

    def test_changed_host_key_fails_at_once(self):
        done, log, took = self.login('changed')
        self.assertEqual(done.returncode, 4)
        self.assertIn('mismatching host key', done.stdout)
        self.assertEqual(len(log.splitlines()), 1)
        self.assertLess(took, 30)


if __name__ == '__main__':
    unittest.main()
