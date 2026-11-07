"""Рой детей и уборка подставного узла не зависят от скорости раннера.

Медленный fork: заглушка порождает рой дольше постоянного предела 5 с, и
предел истекал раньше, чем дети появились («заглушка не породила детей»).
Процесс, что вышел между проверкой alive и kill уборки узла, ронял тест
ProcessLookupError (ERROR вместо проверки).
"""
import io
from pathlib import Path
import unittest
from unittest import mock

import test_ssh_limit_remote_code
from test_ssh_limit_remote_code import Node
import test_ssh_limit_session
from test_ssh_limit_session import FAKE_SSH

SWARM_LOOP = 'for ((i = 0; i < FAKE_MANY / 2; i++)); do sleep "$FAKE_SLEEP"'
# Ребёнок раз в 7 мс: 1000 детей дольше 5 с, как fork на перегруженном раннере.
SLOW_LOOP = 'for ((i = 0; i < FAKE_MANY / 2; i++)); do command sleep 0.007; sleep "$FAKE_SLEEP"'


def free_pid():
    """Номер, которого нет и не будет: pid_max процессу не выдаётся."""
    return int(Path('/proc/sys/kernel/pid_max').read_text())


class SlowRunnerTests(unittest.TestCase):
    def test_swarm_on_slow_fork(self):
        self.assertIn(SWARM_LOOP, FAKE_SSH, 'цикл роя в заглушке сменился - поправь подмену')
        with mock.patch.object(test_ssh_limit_session, 'FAKE_SSH', FAKE_SSH.replace(SWARM_LOOP, SLOW_LOOP)):
            stream = io.StringIO()
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
                test_ssh_limit_session.SshLimitSessionTests('test_11_swarm_returns_in_time'))
        self.assertTrue(result.wasSuccessful(), stream.getvalue())

    def test_cleanup_survives_process_gone_after_check(self):
        node = Node('')
        with mock.patch.object(test_ssh_limit_remote_code, 'alive', return_value=[free_pid()]):
            node.__exit__(None, None, None)
        self.assertFalse(node.root.exists(), 'каталог узла не убран')


if __name__ == '__main__':
    unittest.main()
