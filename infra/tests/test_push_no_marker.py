"""push не считает успехом выход ssh с кодом 0, если shell узла не дошёл до команды.

Подставной ssh ведёт себя как sshd с login shell: сначала исполняет .bashrc
узла (режим из FAKE_BASHRC), затем команду через bash. Если .bashrc делает
exit или exec, команда не выполняется и метка _SSH_MARK не печатается, а ssh
выходит с 0. /tmp/anysda узла перенесён во временный каталог.
"""
import os
from pathlib import Path
import tempfile
import unittest

from test_deploy import ROOT, bash

FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
cmd=${cmd//\/tmp\/anysda/$FAKE_NODE/anysda}
# Вход дочитывает stdin, чтобы tar перед ssh не ловил SIGPIPE: проверяется код
# ssh, а не гонка трубы.
case ${FAKE_BASHRC:-} in
  exit)  cat >/dev/null; exit 0 ;;
  exec)  printf 'Welcome to node\n'; cat >/dev/null; exec true ;;
  noise) printf 'NOISE\nmotd line\n' ;;
esac
exec bash -c "$cmd"
'''
FAKE_SSHPASS = '#!/bin/bash\nshift 2\nexec ssh "$@"\n'


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

    def push(self, body):
        exports = ''.join(f"export {k}='{v}'\n" for k, v in self.env.items())
        return bash(exports + f'''DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_USER=root SSH_HOST=entry.example SSH_PASS=fixture
printf 'payload\\n' > payload
mkdir -p bundle/sub && printf 'inner\\n' > bundle/sub/file
''' + body, self.directory.name)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.directory.cleanup()


PUSH_FILE = 'rc=0; push payload || rc=$?; echo "rc=$rc"\n'
PUSH_DIR = 'rc=0; push bundle || rc=$?; echo "rc=$rc"\n'


class PushNoMarkerTests(unittest.TestCase):
    def assert_refused(self, bashrc, body, target):
        with Node(bashrc) as node:
            result = node.push(body)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('rc=0', result.stdout,
                             'push вернул 0, хотя shell узла не выполнил команду')
            self.assertRegex(result.stderr, r'\.bashrc|не дош[её]л|не выполнил',
                             'stderr не называет причину')
            self.assertNotIn('повтор', result.stderr, 'сбой не транзиентный, повторов быть не должно')
            self.assertFalse((node.anysda / target).exists())

    def test_file_push_fails_when_bashrc_exits(self):
        self.assert_refused('exit', PUSH_FILE, 'payload')

    def test_dir_push_fails_when_bashrc_exits(self):
        self.assert_refused('exit', PUSH_DIR, 'bundle')

    def test_push_fails_when_bashrc_prints_and_execs(self):
        self.assert_refused('exec', PUSH_FILE, 'payload')
        self.assert_refused('exec', PUSH_DIR, 'bundle')

    def test_push_succeeds_through_bashrc_noise(self):
        with Node('noise') as node:
            result = node.push(PUSH_FILE + PUSH_DIR)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'rc=0\nrc=0\n', result.stderr)
            self.assertEqual((node.anysda / 'payload').read_text(), 'payload\n')
            self.assertEqual((node.anysda / 'bundle/sub/file').read_text(), 'inner\n')

    def test_push_succeeds_on_plain_node(self):
        with Node('') as node:
            result = node.push(PUSH_FILE)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'rc=0\n', result.stderr)
            self.assertEqual((node.anysda / 'payload').read_text(), 'payload\n')


if __name__ == '__main__':
    unittest.main()
