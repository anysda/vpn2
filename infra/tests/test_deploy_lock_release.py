"""Замок деплоя на машине держит сам ./deploy.sh и отпускает его своим выходом.

Возврат со стенда: после выхода ./deploy.sh замок ещё секунды держал оставшийся
фоновый помощник стадии (дескриптор замка наследовали все потомки), и запуск
следом получал «deploy уже выполняется». Тест гоняет настоящий ./deploy.sh из
копии дерева; infra/deploy.sh подставной и оставляет после себя фоновый процесс.
"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from test_deploy import ROOT

BUSY = 'deploy уже выполняется на этой машине'
# Помощник переживает оркестратор на 20 с: столько замок держался бы по наследству.
HELPER = 'sleep 20 >/dev/null 2>&1 &\n'


class Checkout:
    """Копия ./deploy.sh с подставным infra/deploy.sh; состояние и замок во временном каталоге."""

    def __init__(self, orchestrator):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copyfile(ROOT / 'deploy.sh', self.root / 'deploy.sh')
        (self.root / 'config.yaml').touch()
        (self.root / 'infra/lib').mkdir(parents=True)
        shutil.copyfile(ROOT / 'infra/lib/state-dir.sh', self.root / 'infra/lib/state-dir.sh')
        (self.root / 'infra/lib/config2env.py').write_text('')
        stub = self.root / 'infra/deploy.sh'
        stub.write_text('#!/bin/bash\n' + orchestrator)
        stub.chmod(0o755)
        self.lock = self.root / 'deploy.lock'
        self.env = {**os.environ, 'VPN2_STATE_DIR': f'{self.root}/state', 'VPN2_DEPLOY_LOCK': str(self.lock),
                    'MARK': str(self.root)}

    def start(self, *args):
        return subprocess.Popen(['bash', str(self.root / 'deploy.sh'), *args], env=self.env, text=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def run(self, *args):
        return subprocess.run(['bash', str(self.root / 'deploy.sh'), *args], env=self.env, text=True,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=30)

    def wait_for(self, name, seconds=5):
        deadline = time.monotonic() + seconds
        while not (self.root / name).exists():
            if time.monotonic() > deadline:
                raise AssertionError(f'{name} не появился за {seconds} с')
            time.sleep(0.01)
        return (self.root / name).read_text()

    def lock_is_free(self):
        return subprocess.run(['flock', '-n', str(self.lock), 'true'], check=False).returncode == 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        subprocess.run(['pkill', '-f', f'^sleep .*{self.root.name}'], check=False)
        self.directory.cleanup()


class DeployLockReleaseTests(unittest.TestCase):
    def test_next_run_starts_at_once_twenty_times(self):
        with Checkout(HELPER + 'echo "run $*"\n') as tree:
            for attempt in range(20):
                done = tree.run('30-frontend', 'ru')
                output = done.stdout + done.stderr
                self.assertNotIn(BUSY, output, f'запуск {attempt + 1}')
                self.assertEqual((done.returncode, done.stdout), (0, 'run 30-frontend ru\n'), output)
            self.assertTrue(tree.lock_is_free())

    def test_next_run_starts_at_once_after_failed_run(self):
        with Checkout(HELPER + 'echo fatal >&2\nexit 7\n') as tree:
            for _ in range(3):
                done = tree.run()
                self.assertEqual((done.returncode, done.stderr), (7, 'fatal\n'), done.stdout + done.stderr)

    def test_children_of_orchestrator_do_not_hold_lock_descriptor(self):
        with Checkout('ls -l /proc/$$/fd > "$MARK/fds"\n') as tree:
            done = tree.run()
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertNotIn(str(tree.lock), (tree.root / 'fds').read_text())

    def test_simultaneous_run_is_still_refused(self):
        with Checkout('touch "$MARK/started"\nsleep 2\n') as tree:
            first = tree.start()
            try:
                tree.wait_for('started')
                second = tree.run()
                self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
                self.assertIn(BUSY, second.stderr)
                self.assertFalse(tree.lock_is_free())
            finally:
                first.communicate(timeout=10)
            self.assertEqual(first.returncode, 0)
            self.assertTrue(tree.lock_is_free())

    def test_killed_run_frees_lock_and_stops_orchestrator(self):
        # SIGKILL ловушек не оставляет: замок уходит вместе с процессом, оркестратор получает TERM.
        with Checkout('echo $$ > "$MARK/pid"\ntrap \'touch "$MARK/term"; exit 143\' TERM\n'
                      'sleep 20 >/dev/null 2>&1 & wait $!\n') as tree:
            first = tree.start()
            orchestrator = int(tree.wait_for('pid'))
            self.assertFalse(tree.lock_is_free())
            first.send_signal(signal.SIGKILL)
            first.communicate(timeout=10)
            started = time.monotonic()
            self.assertTrue(tree.lock_is_free(), 'замок остался за убитым ./deploy.sh')
            tree.wait_for('term')
            while os.path.exists(f'/proc/{orchestrator}'):
                self.assertLess(time.monotonic() - started, 5, 'оркестратор пережил убитый ./deploy.sh')
                time.sleep(0.01)

    def test_signal_exit_code_of_orchestrator_is_kept(self):
        with Checkout('kill -TERM $$\n') as tree:
            self.assertEqual(tree.run().returncode, 143)


if __name__ == '__main__':
    unittest.main()
