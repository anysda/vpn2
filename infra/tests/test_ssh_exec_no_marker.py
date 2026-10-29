"""ssh_exec и ssh_fresh не считают успехом выход ssh с кодом 0, если shell узла не дошёл до команды.

Подставной ssh ведёт себя как sshd с login shell: сначала исполняет .bashrc
узла (режим из FAKE_BASHRC), затем команду через bash. Если .bashrc делает
exit или exec, команда не выполняется и метка _SSH_MARK не печатается, а ssh
выходит с 0. /tmp/anysda узла перенесён во временный каталог, каждый вызов
ssh пишет строку в счётчик.
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
  exit)  exit 0 ;;
  exec)  printf 'Welcome to node\n'; exec true ;;
  noise) printf 'NOISE\nmotd line\n' ;;
esac
# Как настоящий ssh: читатель закрыл трубу - остаток вывода отбрасывается,
# код выхода - код команды узла.
bash -c "$cmd" | { cat 2>/dev/null; cat >/dev/null; }
exit "${PIPESTATUS[0]}"
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
        self.written = self.root / 'node' / 'anysda' / 'written'
        self.env = {'PATH': f'{self.root}/bin:{os.environ["PATH"]}',
                    'FAKE_NODE': str(self.root / 'node'), 'FAKE_BASHRC': bashrc}

    def bash(self, body):
        exports = ''.join(f"export {k}='{v}'\n" for k, v in self.env.items())
        return bash(exports + f'''DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_USER=root SSH_HOST=entry.example SSH_PASS=fixture SSH_PROBE_TIMEOUT=5
''' + body, self.directory.name)

    def calls(self):
        path = self.root / 'node' / 'calls'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.directory.cleanup()


class SshExecNoMarkerTests(unittest.TestCase):
    def assert_refused(self, bashrc, helper):
        with self.subTest(bashrc=bashrc, helper=helper), Node(bashrc) as node:
            result = node.bash(f'rc=0; {helper} "{WRITE}" || rc=$?; echo "rc=$rc"\n')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('rc=0', result.stdout,
                             f'{helper} вернул 0, хотя shell узла не выполнил команду')
            self.assertRegex(result.stderr, REASON, 'stderr не называет причину')
            self.assertNotIn('повтор', result.stderr, 'сбой не транзиентный, повторов быть не должно')
            self.assertEqual(node.calls(), 1, 'ssh вызван больше одного раза')
            self.assertFalse(node.written.exists())

    def test_exec_and_fresh_fail_when_bashrc_exits(self):
        for helper in ('ssh_exec', 'ssh_fresh'):
            self.assert_refused('exit', helper)

    def test_exec_and_fresh_fail_when_bashrc_prints_and_execs(self):
        for helper in ('ssh_exec', 'ssh_fresh'):
            self.assert_refused('exec', helper)

    def test_caller_under_set_e_stops(self):
        for helper in ('ssh_exec', 'ssh_fresh'):
            with self.subTest(helper=helper), Node('exit') as node:
                result = node.bash(f'set -e\n{helper} "{WRITE}"\necho after\n')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('after', result.stdout)
                self.assertRegex(result.stderr, REASON)
                self.assertFalse(node.written.exists())

    def test_command_output_and_codes_pass_through(self):
        for bashrc in ('noise', ''):
            with self.subTest(bashrc=bashrc), Node(bashrc) as node:
                result = node.bash(f'''set -e
[[ "$(ssh_exec 'echo hi')" == hi ]] || {{ echo bad-exec >&2; exit 1; }}
[[ "$(ssh_fresh 'echo fresh')" == fresh ]] || {{ echo bad-fresh >&2; exit 1; }}
ssh_exec "{WRITE}"
rc=0; out=$(ssh_exec 'echo before; exit 3') || rc=$?
[[ $rc == 3 && $out == before ]] || {{ echo "bad-exec-rc $rc $out" >&2; exit 1; }}
rc=0; ssh_fresh 'exit 4' || rc=$?
[[ $rc == 4 ]] || {{ echo "bad-fresh-rc $rc" >&2; exit 1; }}
echo all-clean
''')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'all-clean\n', result.stderr)
                self.assertEqual(node.written.read_text(), 'done\n')
                self.assertNotRegex(result.stderr, REASON)

    def test_reader_closing_early_is_not_missing_marker(self):
        with Node('') as node:
            result = node.bash(r'''for i in $(seq 20); do
  { rc=0; ssh_exec 'head -c 2000000 /dev/zero | tr "\0" x' || rc=$?; echo "rc=$rc" >&2; } | head -c1 >/dev/null
done
''')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotRegex(result.stderr, REASON, 'ранний разрыв трубы принят за отсутствие метки')
            self.assertEqual(result.stderr.count('rc=0'), 20, result.stderr)


if __name__ == '__main__':
    unittest.main()
