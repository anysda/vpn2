"""Deploy, поставленный оператором на паузу (Ctrl-Z), не теряет замок на entry.

Ctrl-Z шлёт SIGTSTP всей группе процессов задания в терминале. Если пульс
замка и клиент ssh в той же группе, они встают вместе с deploy, держатель на
entry через окно тишины отпускает замок, второй deploy его берёт, а после fg
первый идёт дальше уже без замка. Подставной ssh тот же, что в тесте
осиротевшей сессии: команда замка идёт в отдельной сессии, как у sshd на
entry, и SIGTSTP её не касается. Deploy запускается в своей группе процессов
той же сессии - так shell с job control запускает задание.
"""
import os
import re
import signal
import subprocess
import time
import unittest

from test_deploy import ROOT
from test_deploy_lock_orphan_session import CUT, NEXT, OrphanEntry, wait_for
from test_deploy_lock_ssh_failure import START, verdict

PULSE_WINDOW = int(re.search(r'LOCK_PULSE_WINDOW=(\d+)',
                             (ROOT / 'infra/lib/deploy-release.sh').read_text())[1])
# Пауза заметно дольше окна тишины держателя на entry.
PAUSE = PULSE_WINDOW + 5

PAUSED = '''release_start rollback
echo deploy-locked
: > "$FAKE_NODE/locked"
while [[ ! -e $FAKE_NODE/go ]]; do command sleep 0.2; done
echo deploy-end
'''


class JobEntry(OrphanEntry):
    def start(self, body):
        exports = ''.join(f"export {k}='{v}'\n" for k, v in self.env.items())
        script = exports + f'''DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_USER=root SSH_HOST=entry.example SSH_PASS=fixture SSH_PROBE_TIMEOUT=5
''' + START + CUT + body
        return subprocess.Popen(['bash', '-euo', 'pipefail', '-c', script],
                                cwd=self.directory.name, text=True, process_group=0,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class DeployLockStoppedDeployTests(unittest.TestCase):
    def test_deploy_paused_with_ctrl_z_keeps_entry_lock(self):
        with JobEntry() as entry:
            first = entry.start(PAUSED)
            try:
                if not wait_for(entry.node / 'locked', 15):
                    first.kill()
                    self.fail('первый deploy не взял замок:\n' + first.communicate()[1])
                os.killpg(first.pid, signal.SIGTSTP)
                time.sleep(PAUSE)
                held = entry.run(NEXT)
                os.killpg(first.pid, signal.SIGCONT)
                self.assertNotIn('deploy-locked', held.stdout,
                                 f'deploy на паузе {PAUSE} с потерял замок: второй deploy '
                                 f'взял его\n{held.stderr}')
                self.assertNotEqual(held.returncode, 0, held.stderr)
                self.assertIn('deploy already running', verdict(held.stderr), held.stderr)
                (entry.node / 'go').touch()
                out, err = first.communicate(timeout=15)
            finally:
                if first.poll() is None:
                    os.killpg(first.pid, signal.SIGKILL)
                    first.communicate()
            self.assertEqual(first.returncode, 0, err)
            self.assertIn('deploy-end', out)
            self.assertNotIn('FATAL', err)

            after = entry.run(NEXT)
            self.assertEqual(after.returncode, 0,
                             f'после конца deploy замок не свободен:\n{after.stderr}')
            self.assertIn('deploy-locked', after.stdout)


if __name__ == '__main__':
    unittest.main()
