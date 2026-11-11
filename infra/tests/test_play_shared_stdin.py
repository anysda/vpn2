"""Запуски ansible не делят stdin деплоя с соседями.

ssh без терминала ставит O_NONBLOCK на свои stdin/stdout/stderr. Флаг живёт на
открытом файле, а не на номере дескриптора: все, кто унаследовал тот же stdin
деплоя, видят его неблокирующим. Команды ansible при старте отказывают с
«Ansible requires blocking IO on stdin/stdout/stderr», если хоть один из трёх
неблокирующий. Узлы стадии идут параллельно, поэтому сосед (ssh пульса, ssh
другой цепочки) успевал испортить stdin до проверки.
"""
import fcntl
import os
from pathlib import Path
import pty
import subprocess
import tempfile
import unittest

from play_stage import site_tree


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
ROUNDS = 20
NODES = ('ru', 'us', 'de')

# Та же проверка, что у ansible.cli.check_blocking_io.
CHECK = '''#!/usr/bin/env python3
import os, sys
bad = [name for fd, name in ((0, '<stdin>'), (1, '<stdout>'), (2, '<stderr>'))
       if not os.get_blocking(fd)]
if bad:
    print('ERROR: Ansible requires blocking IO on stdin/stdout/stderr. '
          'Non-blocking file handles detected: ' + ', '.join(bad))
    sys.exit(1)
print('ok')
'''

# Сосед: как ssh, переводит унаследованные дескрипторы в неблокирующий режим.
NEIGHBOUR = "python3 -c 'import os\nfor fd in (0, 1, 2): os.set_blocking(fd, False)'"

STUBS = f'''
DEPLOY_ROOT=$PWD
FW_GUARD_STAGES=''
die() {{ echo "die: $*" >&2; exit 1; }}
log() {{ :; }}
ok() {{ :; }}
warn() {{ :; }}
fw_guard_stage() {{ return 1; }}
source {ROOT}/infra/lib/ansible-venv.sh
: > wheel.whl
ansible_wheel_build() {{ :; }}
ansible_wheel_path() {{ echo "$PWD/wheel.whl"; }}
load_env() {{ HOST_TAG=$1; }}
push() {{ :; }}
ssh_exec() {{ {NEIGHBOUR}; }}
'''

PLAYS = f'''
ansible_venv_install() {{ :; }}
for round in $(seq {ROUNDS}); do
  pids=()
  for node in {' '.join(NODES)}; do
    ( run_play 42-x "$node" ) >> "out.$node" 2>&1 &
    pids+=("$!")
  done
  for pid in "${{pids[@]}}"; do wait "$pid" || echo "round $round failed" >> failed; done
done
'''


def nonblocking(fd):
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)


class SharedStdinTests(unittest.TestCase):
    def tree(self, directory):
        root = Path(directory)
        site_tree(root, ['42-x'])
        venv = root / 'vpn2/venv'
        (venv / 'bin').mkdir(parents=True)
        (venv / '.vpn2-pins').write_text('x\n')
        for name in ('ansible-playbook', 'ansible-galaxy'):
            (venv / 'bin' / name).write_text(CHECK)
            (venv / 'bin' / name).chmod(0o755)
        return root

    def run_script(self, script, directory, stdin):
        root = Path(directory)
        with open(root / 'deploy.log', 'w') as log:
            # stdout и stderr деплоя тоже общие и тоже испорчены соседом.
            nonblocking(log.fileno())
            return subprocess.run(['bash', '-euo', 'pipefail', '-c', STUBS + script], cwd=directory,
                                  stdin=stdin, stdout=log, stderr=subprocess.STDOUT, timeout=120,
                                  env=dict(os.environ, VPN2_ROOT=str(root / 'vpn2')))

    def assert_plays_pass(self, stdin):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory)
            nonblocking(stdin)
            result = self.run_script(PLAYS, directory, stdin)
            log = (root / 'deploy.log').read_text()
            self.assertEqual(result.returncode, 0, log)
            for node in NODES:
                self.assertEqual((root / f'out.{node}').read_text().splitlines(),
                                 [f'[{node}]ok'] * ROUNDS)
            self.assertFalse((root / 'failed').exists())

    def test_parallel_plays_with_shared_dev_null(self):
        # cron, CI, `setsid nohup ./deploy.sh < /dev/null`: терминала нет.
        fd = os.open('/dev/null', os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.assert_plays_pass(fd)

    def test_parallel_plays_with_shared_pipe(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        self.assert_plays_pass(read)

    def test_parallel_plays_under_pseudo_terminal(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        self.assert_plays_pass(slave)

    def test_venv_install_does_not_read_deploy_stdin(self):
        # ansible-galaxy и pip установки venv идут рядом с ssh pre-flight.
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory)
            fd = os.open('/dev/null', os.O_RDONLY)
            self.addCleanup(os.close, fd)
            nonblocking(fd)
            script = f'''
_ansible_venv_install_locked() {{ "$VPN2_ROOT/venv/bin/ansible-galaxy" > "$PWD/galaxy" 2>&1; }}
{NEIGHBOUR}
ansible_venv_install
'''
            result = self.run_script(script, directory, fd)
            self.assertEqual((root / 'galaxy').read_text(), 'ok\n')
            self.assertEqual(result.returncode, 0, (root / 'deploy.log').read_text())


if __name__ == '__main__':
    unittest.main()
