"""Shell узла не дошёл до команды при любом коде выхода .bashrc, не только при 0.

Частый случай - `exec tmux` или `exit 1` в .bashrc root без терминала: ssh
выходит с 1, метки _SSH_MARK нет. push, ssh_exec и ssh_fresh должны падать с
названной причиной и без повторов, а не принимать 1 за код команды узла.
Сбой соединения (255) повторяется как раньше.
"""
import os
from pathlib import Path
import tempfile
import unittest

from test_deploy import ROOT, bash

FAKE_SSH = r'''#!/bin/bash
echo call >> "$FAKE_NODE/calls"
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
cmd=${cmd//\/tmp\/anysda/$FAKE_NODE/anysda}
case ${FAKE_BASHRC:-} in
  exit1) cat >/dev/null; exit 1 ;;
  exec1) printf 'Welcome to node\n'; cat >/dev/null; exec false ;;
  conn)  echo 'ssh: connect to host entry.example port 22: Connection refused' >&2; exit 255 ;;
esac
exec bash -c "$cmd"
'''
FAKE_SSHPASS = '#!/bin/bash\nshift 2\nexec ssh "$@"\n'
WRITE = "mkdir -p /tmp/anysda && echo done > /tmp/anysda/written"
REASON = r'\.bashrc.*|не дош[её]л|не выполнил'


class Node:
    def __init__(self, bashrc):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'bin').mkdir()
        (self.root / 'node').mkdir()
        for name, text in (('ssh', FAKE_SSH), ('sshpass', FAKE_SSHPASS)):
            (self.root / 'bin' / name).write_text(text)
            (self.root / 'bin' / name).chmod(0o755)
        self.anysda = self.root / 'node' / 'anysda'
        self.env = {'PATH': f'{self.root}/bin:{os.environ["PATH"]}',
                    'FAKE_NODE': str(self.root / 'node'), 'FAKE_BASHRC': bashrc}

    def bash(self, body):
        exports = ''.join(f"export {k}='{v}'\n" for k, v in self.env.items())
        return bash(exports + f'''DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_USER=root SSH_HOST=entry.example SSH_PASS=fixture SSH_PROBE_TIMEOUT=5
printf 'payload\\n' > payload
mkdir -p bundle/sub && printf 'inner\\n' > bundle/sub/file
''' + body, self.directory.name)

    def calls(self):
        path = self.root / 'node' / 'calls'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.directory.cleanup()


CALLS = {
    'push payload': 'payload',
    'push bundle': 'bundle',
    f'ssh_exec "{WRITE}" </dev/null': 'written',
    f'ssh_fresh "{WRITE}" </dev/null': 'written',
}


class BashrcNonzeroTests(unittest.TestCase):
    def assert_refused(self, bashrc):
        for call, target in CALLS.items():
            with self.subTest(bashrc=bashrc, call=call), Node(bashrc) as node:
                result = node.bash(f'rc=0; {call} || rc=$?; echo "rc=$rc"\n')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('rc=0', result.stdout)
                self.assertNotIn('rc=1\n', result.stdout,
                                 'код .bashrc отдан как код команды узла')
                self.assertRegex(result.stderr, REASON, 'stderr не называет причину')
                self.assertNotIn('повтор', result.stderr, 'сбой не транзиентный, повторов быть не должно')
                self.assertEqual(node.calls(), 1, 'ssh вызван больше одного раза')
                self.assertFalse((node.anysda / target).exists())

    def test_bashrc_exit_1(self):
        self.assert_refused('exit1')

    def test_bashrc_exec_program_exiting_1(self):
        self.assert_refused('exec1')

    def test_connection_failure_still_retried(self):
        for call in ('push payload', f'ssh_exec "{WRITE}" </dev/null'):
            with self.subTest(call=call), Node('conn') as node:
                result = node.bash(f'rc=0; {call} || rc=$?; echo "rc=$rc"\n')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('rc=255', result.stdout, result.stderr)
                self.assertIn('повтор 4/4', result.stderr)
                self.assertNotRegex(result.stderr, REASON)


if __name__ == '__main__':
    unittest.main()
