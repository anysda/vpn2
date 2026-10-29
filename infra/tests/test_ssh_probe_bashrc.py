"""ssh_probe (pre-flight) видит, что shell узла не дошёл до команды, как push.

Подставной ssh ведёт себя как sshd с login shell: сначала .bashrc узла (режим
из FAKE_BASHRC), затем команда через bash. `exit 0` в .bashrc - ssh выходит с
0, команда не выполнена; `exit 1` - ssh выходит с 1. ssh_probe должен отдать
$_SSH_NO_MARK (254) с причиной про .bashrc, а pre-flight - не повторять.
Шумный .bashrc, сбой сети и отказ во входе классифицируются как раньше.
"""
import os
from pathlib import Path
import re
import tempfile
import unittest

from test_deploy import ROOT, bash
from test_ssh_refusal import PREFLIGHT

FAKE_SSH = r'''#!/bin/bash
echo call >> "$FAKE_NODE/calls"
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
case ${FAKE_BASHRC:-} in
  exit0) exit 0 ;;
  exit1) exit 1 ;;
  noise) printf 'Welcome to node\nmotd line\n'; printf 'bashrc warning\n' >&2 ;;
  conn)  echo 'ssh: connect to host entry.example port 22: Connection refused' >&2; exit 255 ;;
  pass)  echo 'Permission denied, please try again.' >&2; exit 5 ;;
  key)   echo 'root@entry.example: Permission denied (publickey).' >&2; exit 255 ;;
esac
bash -c "$cmd"
'''
FAKE_SSHPASS = '#!/bin/bash\nshift 2\nexec ssh "$@"\n'
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
        self.env = {'PATH': f'{self.root}/bin:{os.environ["PATH"]}',
                    'FAKE_NODE': str(self.root / 'node'), 'FAKE_BASHRC': bashrc}

    def bash(self, body):
        exports = ''.join(f"export {k}='{v}'\n" for k, v in self.env.items())
        return bash(exports + f'''DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
# Паузы не ждём, но часы цикла они двигают, как настоящие.
sleep() {{ SECONDS=$((SECONDS + $1)); }}
SSH_USER=root SSH_HOST=entry.example SSH_PASS=fixture SSH_PROBE_TIMEOUT=5
''' + body, self.directory.name)

    def probe(self):
        """(код ssh_probe, его stdout - причина, stderr, вызовов ssh)."""
        result = self.bash('rc=0; reason=$(ssh_probe) || rc=$?\n'
                           'printf "rc=%s\\nreason=%s\\n" "$rc" "$reason"\n')
        if result.returncode:
            raise AssertionError(f'сценарий упал: {result.stderr}')
        rc = int(re.search(r'^rc=(\d+)$', result.stdout, re.M).group(1))
        reason = result.stdout.split('reason=', 1)[1]
        return rc, reason, result.stderr, self.calls()

    def preflight(self):
        result = self.bash(PREFLIGHT)
        return result.stdout + result.stderr, self.calls()

    def push_reason(self):
        (self.root / 'payload').write_text('payload\n')
        result = self.bash('push payload || :\n')
        return result.stderr

    def calls(self):
        path = self.root / 'node' / 'calls'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.directory.cleanup()


class SshProbeBashrcTests(unittest.TestCase):
    def assert_no_mark(self, bashrc):
        with Node(bashrc) as node:
            rc, reason, err, calls = node.probe()
            self.assertEqual(rc, 254, f'ssh_probe принял .bashrc {bashrc} за ответ ssh: '
                                      f'rc={rc} reason={reason!r} stderr={err!r}')
            self.assertRegex(reason + err, REASON, 'причина не названа')
            self.assertEqual(calls, 1)
        # Та же причина, что у push на этой же ноде.
        with Node(bashrc) as node:
            self.assertRegex(node.push_reason(), REASON)
        with Node(bashrc) as node:
            out, calls = node.preflight()
            self.assertNotIn('✓', out, 'pre-flight счёл ноду доступной')
            self.assertRegex(out, REASON, 'pre-flight не называет .bashrc')
            self.assertEqual(calls, 1, f'pre-flight повторял пробу:\n{out}')
            self.assertNotIn('попытка 2', out)

    def test_1_bashrc_exit_0_is_not_reachable(self):
        self.assert_no_mark('exit0')

    def test_2_bashrc_exit_1_no_mark_one_attempt(self):
        self.assert_no_mark('exit1')

    def test_3_noisy_bashrc_still_reachable(self):
        with Node('noise') as node:
            rc, reason, err, calls = node.probe()
            self.assertEqual(rc, 0, f'reason={reason!r} stderr={err!r}')
            self.assertEqual(calls, 1)
        with Node('noise') as node:
            out, calls = node.preflight()
            self.assertIn('✓', out)
            self.assertEqual(calls, 1, out)

    def test_4_network_and_auth_classes_unchanged(self):
        for bashrc, code, text in (('conn', 1, 'Connection refused'),
                                   ('pass', 3, 'неверный пароль'),
                                   ('key', 3, 'Permission denied')):
            with self.subTest(bashrc), Node(bashrc) as node:
                rc, reason, err, calls = node.probe()
                self.assertEqual(rc, code, f'reason={reason!r} stderr={err!r}')
                self.assertIn(text, reason)
                self.assertNotRegex(reason + err, REASON)
                self.assertEqual(calls, 1)
        with Node('conn') as node:
            out, calls = node.preflight()
            self.assertEqual(calls, 5, out)
            self.assertNotRegex(out, REASON)


if __name__ == '__main__':
    unittest.main()
