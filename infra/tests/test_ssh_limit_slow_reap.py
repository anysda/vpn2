"""Ctrl-C во время ssh_fresh: убитый внук ещё не пожат init, группа пуста.

Сон убийцы по пределу _limit_run гасит `pkill -KILL -P`: он уходит к init
зомби, и на загруженном раннере init пожинает его не сразу. kill -0 на группу
видит и зомби. Здесь медленный init - сам тест: subreaper, который не
пожинает чужих детей до проверки. Живой процесс в группе после Ctrl-C - по-
прежнему отказ, зомби - нет.
"""
import ctypes
import os
import unittest

import test_ssh_limit_session as session
from test_deploy import ROOT

PR_SET_CHILD_SUBREAPER = 36


def subreaper(on):
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, int(on), 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'prctl(PR_SET_CHILD_SUBREAPER)')


def reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if not pid:
            return


class SshFreshSlowReapTests(unittest.TestCase):
    def test_ssh_fresh_int_with_unreaped_zombie(self):
        script = (f'DEPLOY_ROOT={ROOT}/infra\nsource {ROOT}/infra/lib/ssh.sh\n'
                  'SSH_USER=root SSH_HOST=node.example SSH_PASS=fixture\n'
                  'out=$(ssh_fresh echo ok 2>&1) || echo "rc=$?"\n')
        subreaper(True)
        try:
            with session.StubNode('orphan') as node:
                result = session.SshLimitSessionTests.interrupt(
                    self, script, {**node.env, 'SSH_PROBE_TIMEOUT': str(session.HANG)},
                    node.root, node.pids, 'INT')
                session.SshLimitSessionTests.assert_interrupted(self, 'ssh_fresh', 'INT', result)
        finally:
            subreaper(False)
            reap()


if __name__ == '__main__':
    unittest.main()
