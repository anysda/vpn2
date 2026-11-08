"""Помощники «жив ли процесс» в тестах считают пожатый процесс мёртвым.

init может пожать процесс между проверкой и чтением /proc/<pid>/stat: тогда
open или read бросает FileNotFoundError или ProcessLookupError (ESRCH). Чтение
подменено, тайминг не нужен.
"""
import builtins
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import test_deploy_fatal_exit_code
import test_deploy_lock_orphan_session

GONE = (FileNotFoundError(errno.ENOENT, 'No such file or directory'),
        ProcessLookupError(errno.ESRCH, 'No such process'))


def reaped(error, real):
    def read(path, *args, **kwargs):
        if str(path).startswith('/proc/') and str(path).endswith('/stat'):
            raise error
        return real(path, *args, **kwargs)
    return read


class AliveReapedTests(unittest.TestCase):
    def check(self, alive):
        for error in GONE:
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(os, 'kill'), \
                    mock.patch.object(Path, 'read_text', side_effect=error):
                self.assertFalse(alive(os.getpid()))

    def test_orphan_session_alive(self):
        self.check(test_deploy_lock_orphan_session.alive)


class FakeSshReapedCoprocTests(unittest.TestCase):
    """Подставной ssh ждёт зомби coproc замка; пожатый coproc - тоже конец ожидания."""

    def test_reset_with_reaped_coproc_refuses(self):
        for error in GONE:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as node:
                Path(node, 'coproc.pid').write_text('1\n')
                Path(node, 'lock.pgid').write_text('1\n')
                env = {'FAKE_NODE': node, 'FAKE_RESET': 'other:1', 'FAKE_RESET_LOCK': '1'}
                scope = {'__name__': '__main__', 'open': reaped(error, builtins.open)}
                with mock.patch.dict(os.environ, env), mock.patch.object(os, 'killpg'), \
                        mock.patch.object(sys, 'argv', ['ssh', 'host', 'true']), \
                        mock.patch.object(sys, 'stderr'), \
                        self.assertRaises(SystemExit) as stop:
                    exec(test_deploy_fatal_exit_code.FAKE_SSH, scope)
                self.assertEqual(stop.exception.code, 255)


if __name__ == '__main__':
    unittest.main()
