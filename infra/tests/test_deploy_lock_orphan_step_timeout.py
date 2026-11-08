"""Шаг deploy в тесте осиротевшей сессии, не уложившийся в предел, валит тест.

Под полной загрузкой CPU шаг `OrphanEntry.run` может не уложиться в свой
предел. Тогда тест обязан упасть сразу, а не ждать вечно: процессы клиента
подставного ssh (пересылка stdout замка) держат вывод шага открытым, и ран CI
висит до снятия задания, пряча причину.
"""
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import unittest

from test_deploy_lock_orphan_session import OrphanEntry, alive as running

# Предел шага в этом тесте; сам шаг заведомо дольше.
STEP = 3
# Запас на уборку процессов после предела.
GRACE = 10
# Замок взят, затем deploy ждёт без дочерних процессов: держат вывод только
# клиент ssh замка и сам deploy.
SLOW = '''release_start rollback
echo deploy-locked
mkfifo "$FAKE_NODE/hang"
read -r _ <>"$FAKE_NODE/hang"
'''


def fixture_processes(node):
    """Процессы, порождённые шагом: у всех в окружении FAKE_NODE этого теста."""
    mark = f'FAKE_NODE={node}'.encode()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if mark in (proc / 'environ').read_bytes().split(b'\0'):
                yield int(proc.name)
        except OSError:
            pass


def alive(pid):
    # Процесс могут добрать между двумя чтениями /proc.
    try:
        return running(pid)
    except FileNotFoundError:
        return False


def kill_all(pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class OrphanStepTimeoutTests(unittest.TestCase):
    def test_step_over_limit_fails_in_bounded_time(self):
        with OrphanEntry() as entry:
            outcome = {}

            def step():
                try:
                    outcome['result'] = entry.run(SLOW, timeout=STEP)
                except subprocess.TimeoutExpired as error:
                    outcome['timeout'] = error

            worker = threading.Thread(target=step, daemon=True)
            started = time.monotonic()
            worker.start()
            worker.join(STEP + GRACE)
            elapsed = time.monotonic() - started
            if worker.is_alive():
                leftovers = list(fixture_processes(entry.node))
                kill_all(leftovers)
                worker.join(10)
                self.fail(f'шаг сверх предела {STEP} с не кончился за {STEP + GRACE} с: '
                          f'вывод держат процессы подставного ssh ({len(leftovers)} шт.)')

            self.assertLessEqual(elapsed, STEP + GRACE)
            # Превышение предела видно как провал шага, а не как успех.
            if 'result' in outcome:
                self.assertNotEqual(outcome['result'].returncode, 0, outcome['result'].stderr)
            else:
                self.assertIn('timeout', outcome)

            # Клиент ssh замка (сам ssh, пересылка stdin и stdout) не остался
            # сиротой после шага.
            clients = [int(pid) for path in entry.node.glob('session.*/client')
                       for pid in path.read_text().split()]
            self.assertTrue(clients, 'шаг не взял замок через подставной ssh')
            deadline = time.monotonic() + 2
            while any(alive(pid) for pid in clients) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertEqual([pid for pid in clients if alive(pid)], [],
                             'процессы клиента ssh пережили шаг')


if __name__ == '__main__':
    unittest.main()
