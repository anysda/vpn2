"""Текст, который .bashrc узла печатает в stdout при входе по ssh, не ломает разбор вывода.

Подставной ssh ведёт себя как sshd с login shell: сначала печатает текст из
.bashrc, затем выполняет команду через bash. Каталоги узла (замок deploy,
/tmp/anysda) перенесены во временный каталог.
"""
import importlib.util
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import unittest
from unittest import mock

from test_deploy import ROOT, bash

FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
cmd=${cmd//\/var\/lib\/vpn2-deploy/$FAKE_NODE/lock}
cmd=${cmd//\/tmp\/anysda/$FAKE_NODE/anysda}
printf '%b' "$FAKE_NOISE"
[[ -z ${FAKE_LOGIN_EXIT:-} ]] || exit 0
exec bash -c "$cmd"
'''
FAKE_SSHPASS = '#!/bin/bash\nshift 2\nexec ssh "$@"\n'
NOISES = ('NOISE\\n', 'NOISE', 'motd line 1\\nmotd line 2\\n', '')


class Node:
    def __init__(self, noise):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'bin').mkdir()
        (self.root / 'node').mkdir()
        for name, text in (('ssh', FAKE_SSH), ('sshpass', FAKE_SSHPASS)):
            (self.root / 'bin' / name).write_text(text)
            (self.root / 'bin' / name).chmod(0o755)
        self.env = {'PATH': f'{self.root}/bin:{os.environ["PATH"]}',
                    'FAKE_NODE': str(self.root / 'node'), 'FAKE_NOISE': noise}

    def bash(self, body):
        exports = ''.join(f"export {k}='{v}'\n" for k, v in self.env.items())
        return bash(exports + f'''DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_USER=root SSH_HOST=entry.example SSH_PASS=fixture
''' + body, self.directory.name)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.directory.cleanup()


def tool_function(path, name):
    text = (ROOT / path).read_text()
    return re.search(rf'^{name}\(\) \{{\n.*?^\}}\n', text, re.M | re.S).group(0)


class SshLoginNoiseTests(unittest.TestCase):
    def test_deploy_lock_is_confirmed_when_bashrc_prints_to_stdout(self):
        for noise in NOISES:
            with self.subTest(noise=noise), Node(noise) as node:
                result = node.bash(f'''source {ROOT}/infra/lib/deploy-release.sh
install_prereqs() {{ :; }}
load_env() {{ HOST_TAG=$1; }}
expand_hosts() {{ :; }}
VPN2_DEVELOPMENT=true
release_start rollback
test -e "$FAKE_NODE/lock/deploy.lock"
echo deploy-locked
''')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('deploy-locked', result.stdout)
                self.assertNotIn('did not confirm deploy lock', result.stderr)

    def test_lock_failure_names_login_text_when_shell_never_runs_the_command(self):
        with Node('NOISE\\n') as node:
            node.env['FAKE_LOGIN_EXIT'] = '1'
            result = node.bash(f'''source {ROOT}/infra/lib/deploy-release.sh
install_prereqs() {{ :; }}
load_env() {{ HOST_TAG=$1; }}
VPN2_DEVELOPMENT=true
release_start rollback
''')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ssh returned 'NOISE'", result.stderr)
            self.assertIn('.bashrc', result.stderr)

    def test_captured_output_and_exit_code_carry_only_the_command(self):
        for noise in NOISES:
            with self.subTest(noise=noise), Node(noise) as node:
                result = node.bash('''printf 'a b\\n\\nlast' > payload
push payload
[[ "$(ssh_exec 'cat /tmp/anysda/payload')" == "$(cat payload)" ]] || { echo bad-push-cat >&2; exit 1; }
[[ "$(ssh_exec 'echo true')" == true ]] || { echo bad-exec >&2; exit 1; }
[[ "$(ssh_fresh 'echo fresh')" == fresh ]] || { echo bad-fresh >&2; exit 1; }
ssh_exec 'printf partial' > partial
[[ "$(od -An -c partial | tr -d ' ')" == partial ]] || { echo bad-partial >&2; exit 1; }
rc=0; out=$(ssh_exec 'echo before-exit; exit 3') || rc=$?
[[ $rc == 3 && $out == before-exit ]] || { echo "bad-rc $rc $out" >&2; exit 1; }
rc=0; ssh_fresh 'exit 4' || rc=$?
[[ $rc == 4 ]] || { echo "bad-fresh-rc $rc" >&2; exit 1; }
[[ "$(printf 'x\\n' | ssh_exec 'cat')" == x ]] || { echo bad-stdin >&2; exit 1; }
echo all-clean
''')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'all-clean\n')

    def test_verify_traffic_target_ready_after_login_text(self):
        spec = importlib.util.spec_from_file_location('verify_traffic_noise',
                                                      ROOT / 'infra/lib/verify-traffic.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cases = (("printf 'NOISE\\nmotd\\n'; sleep 0.2; echo ready; sleep 5", True),
                 # exit_command начинается с пустой строки: текст без перевода строки не склеивается.
                 ('printf NOISE; echo; echo ready; sleep 5', True),
                 ('printf NOISE; echo ready; sleep 5', False),
                 ("printf 'NOISE\\n'; exit 1", False))
        for command, expected in cases:
            with self.subTest(command=command):
                process = subprocess.Popen(['bash', '-c', command], stdout=subprocess.PIPE, text=True)
                try:
                    self.assertIs(module.Targets.wait_ready(process), expected)
                finally:
                    process.kill()
                    process.wait()
                    process.stdout.close()
        self.assertTrue(module.Targets.exit_command(1, 1).startswith('echo && '))

    def test_speedtest_reads_only_command_output_direct_and_via_hub(self):
        sh = runpy.run_path(str(ROOT / 'infra/tools/speedtest.py'))['sh']
        for noise in NOISES:
            with self.subTest(noise=noise), Node(noise) as node, \
                    mock.patch.dict(os.environ, node.env):
                self.assertEqual(sh('hub', 'echo hi'), 'hi\n')
                self.assertEqual(sh('exit1', 'echo hi; cat', via='hub', stdin='in\n'), 'hi\nin\n')

    def test_tools_read_only_command_output(self):
        functions = (tool_function('infra/tools/emergency-accept.sh', 'node')
                     + tool_function('infra/tools/ci-stand.sh', 'ssh_node'))
        for noise in NOISES:
            with self.subTest(noise=noise), Node(noise) as node:
                result = node.bash(functions + '''declare -A IP=([exit1]=203.0.113.7)
SSH_KEY='' KNOWN_HOSTS='' STAND_DIR=$PWD
[[ "$(node exit1 'echo leader 3')" == 'leader 3' ]] || { echo bad-node-out >&2; exit 1; }
rc=0; node exit1 'exit 5' || rc=$?
[[ $rc == 5 ]] || { echo "bad-node-rc $rc" >&2; exit 1; }
[[ "$(ssh_node 203.0.113.7 'echo 0')" == 0 ]] || { echo bad-stand >&2; exit 1; }
echo tools-clean
''')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'tools-clean\n')


if __name__ == '__main__':
    unittest.main()
