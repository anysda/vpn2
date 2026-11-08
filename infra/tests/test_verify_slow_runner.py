"""Медленный раннер: исправный сценарий 99-verify дольше 20 с - не зависание.

run_verify гоняет весь сценарий с постоянным пределом 20 с. На раннере, где
на ядро приходится 20 и больше процессов, исправный сценарий идёт дольше, и
тест падает TimeoutExpired, хотя проверки сценария верны. Медленный раннер
здесь - один вызов заглушки, который спит 22 с.
"""
import os
import tempfile
import unittest
from unittest import mock

import test_verify

SLOW = '''import time
_slow = os.environ.get('SLOW_ONCE')
if _slow and not Path(_slow).exists():
    Path(_slow).touch()
    time.sleep(22)
'''
ANCHOR = 'from pathlib import Path\n'


class VerifySlowRunnerTests(unittest.TestCase):
    def test_slow_healthy_scenario_is_not_a_hang(self):
        self.assertEqual(test_verify.MOCK.count(ANCHOR), 1, 'импорты заглушки не найдены')
        slow = test_verify.MOCK.replace(ANCHOR, ANCHOR + SLOW)
        with tempfile.TemporaryDirectory(prefix='verify-slow-') as directory, \
                mock.patch.object(test_verify, 'MOCK', slow):
            once = os.path.join(directory, 'slept')
            result = test_verify.VerifyTest.run_verify(self, extra_env={'SLOW_ONCE': once})
            self.assertTrue(os.path.exists(once), 'заглушка не спала: сценарий не медленный')
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == '__main__':
    unittest.main()
