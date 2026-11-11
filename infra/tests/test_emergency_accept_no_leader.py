"""emergency-accept.sh без лидера: отказ с причиной, узлы не тронуты.

Сценарии, которым нужен лидер, брали его тег без проверки. Нет лидера - тег
пустой, и инструмент падал на `IP[$tag]: unbound variable` уже после того,
как часть действий на узлах сделана (recover успевал поднять entry).

Подставной ssh команд не выполняет: пишет их в журнал и отвечает как узел,
на котором агент не лидер.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT

TOOL = ROOT / 'infra/tools/emergency-accept.sh'
FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
printf '%s\n' "$*" >> "$FAKE_LOG"
printf '\nvpn2-ssh-output-begin\n'
case "$*" in
  *'python3 - /var/lib/vpn2-emergency') echo "${FAKE_ROLE:-observer} 1 false false closed - - -" ;;
esac
'''
NEED_LEADER = (('recover',), ('recover-cancel',), ('sessions-blind', '1'), ('entry-not-ready', '1'),
               ('client', 'up', '1'), ('indicator',))


class NoLeaderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'bin').mkdir()
        (self.root / 'bin/ssh').write_text(FAKE_SSH)
        (self.root / 'bin/ssh').chmod(0o755)
        (self.root / 'nodes.txt').write_text('entry 203.0.113.1\nnl 203.0.113.2\nde 203.0.113.3\n')
        self.log = self.root / 'ssh.log'
        self.env = dict(os.environ, PATH=f'{self.root}/bin:{os.environ["PATH"]}', FAKE_LOG=str(self.log))

    def tool(self, *command, **env):
        self.log.write_text('')
        return subprocess.run(['bash', str(TOOL), '-n', str(self.root / 'nodes.txt'), '-e', 'entry', *command],
                              capture_output=True, text=True, timeout=60, env=self.env | env,
                              stdin=subprocess.DEVNULL, check=False)

    def actions(self):
        """Команды, ушедшие на узлы, кроме чтения состояния агента."""
        return [line for line in self.log.read_text().splitlines()
                if line and not line.endswith('python3 - /var/lib/vpn2-emergency')]

    def test_no_leader_is_refused_with_reason_before_any_action(self):
        for command in NEED_LEADER:
            with self.subTest(command=' '.join(command)):
                result = self.tool(*command)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('нужен один лидер', result.stderr)
                self.assertNotIn('unbound variable', result.stderr)
                self.assertEqual(self.actions(), [], 'узлы тронуты до отказа')

    def test_two_leaders_are_refused_too(self):
        result = self.tool('recover', FAKE_ROLE='leader')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("нужен один лидер, есть: 'nl de'", result.stderr)
        self.assertEqual(self.actions(), [])

    def test_status_without_leader_still_reports(self):
        result = self.tool('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('observer', result.stdout)


if __name__ == '__main__':
    unittest.main()
