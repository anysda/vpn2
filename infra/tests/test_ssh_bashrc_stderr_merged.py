"""`exec 2>&1` в .bashrc узла не приносит метку в вывод ssh_exec и push.

Такой .bashrc сливает stderr команды узла с её stdout. Метка, которую
команда печатает в stderr для _ssh_errfilter, тогда приходит в stdout
вслед за меткой stdout, и _ssh_output отдаёт её вызывающему:
`[[ "$(ssh_exec 'cat …/secure' 2>/dev/null)" == true ]]` в deploy ломается.
Вывод должен быть тем же, что без метки stderr: ровно вывод команды, её
stderr - в том же канале, что stdout, код команды сохраняется.

Узел - тот же настоящий sshd без root, что в test_ssh_bashrc_stderr.py.
"""
from itertools import product
import os
import subprocess
import unittest

import test_ssh_bashrc_stderr as base
from test_deploy import ROOT

MARK = 'vpn2-ssh-output-begin'
NOISE = 'NOISE-merged'
BASHRC = {
    'merge': 'exec 2>&1\n',
    'noise-merge': f'echo {NOISE}-out\necho {NOISE}-err >&2\nexec 2>&1\necho {NOISE}-late\n',
}
CASES = list(product(BASHRC, ('us', 'ru')))


class SshBashrcStderrMergedTests(unittest.TestCase):
    setUpClass = base.SshBashrcStderrTests.__dict__['setUpClass']
    tearDownClass = base.SshBashrcStderrTests.__dict__['tearDownClass']

    def run_ssh(self, body, bashrc, tag):
        (self.home / '.bashrc').write_text(BASHRC[bashrc])
        (self.root / 'deploy' / '.known_hosts').write_text('')
        env = {**os.environ, 'PATH': f'{self.root}/bin:{os.environ["PATH"]}',
               'FAKE_ROOT': str(self.root), 'FAKE_NODE': str(self.root / 'node'),
               'FAKE_SSHD_PORT': str(self.port), 'HOST_TAG': tag}
        script = f'''DEPLOY_ROOT={self.root}/deploy
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_USER=$(id -un) SSH_HOST=node.example SSH_PASS=fixture SSH_PROBE_TIMEOUT=10
''' + body
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.root,
                                env=env, text=True, capture_output=True, timeout=60,
                                stdin=subprocess.DEVNULL)
        self.assertNotIn(MARK, result.stdout, 'метка ssh дошла до stdout вызывающего')
        self.assertNotIn(MARK, result.stderr, 'метка ssh дошла до stderr вызывающего')
        self.assertNotIn(NOISE, result.stdout, 'вывод .bashrc узла дошёл до stdout вызывающего')
        return result

    def test_exec_output_is_exactly_command_output(self):
        for bashrc, tag in CASES:
            with self.subTest(bashrc=bashrc, tag=tag):
                result = self.run_ssh('''[[ "$(ssh_exec 'echo hi' 2>/dev/null)" == hi ]] || echo bad-subst
ssh_exec 'echo hi'
''', bashrc, tag)
                self.assertEqual((result.returncode, result.stdout), (0, 'hi\n'), result.stderr)

    def test_exec_keeps_command_stderr_and_code(self):
        for bashrc, tag in CASES:
            with self.subTest(bashrc=bashrc, tag=tag):
                result = self.run_ssh('''rc=0; ssh_exec 'echo out; echo err >&2; exit 3' || rc=$?
echo "rc=$rc"
''', bashrc, tag)
                self.assertEqual((result.returncode, result.stdout), (0, 'out\nerr\nrc=3\n'),
                                 result.stderr)

    def test_push_prints_nothing(self):
        src = self.root / 'payload-merged'
        src.write_text('data\n')
        written = self.root / 'node' / 'anysda' / 'payload-merged'
        for bashrc, tag in CASES:
            with self.subTest(bashrc=bashrc, tag=tag):
                written.unlink(missing_ok=True)
                result = self.run_ssh(f'''push {src}
echo pushed
''', bashrc, tag)
                self.assertEqual((result.returncode, result.stdout), (0, 'pushed\n'), result.stderr)
                self.assertEqual(written.read_text(), 'data\n')


if __name__ == '__main__':
    unittest.main()
