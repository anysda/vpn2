"""exit в .bashrc узла не прячется за флапом сети и не пропускает экзит.

Подставной ssh ведёт себя как sshd с login shell: первые FAKE_FLAPS вызовов
рвут соединение (255), дальше на узле FAKE_BASHRC_HOST .bashrc делает exit
(режим FAKE_BASHRC), на прочих узлах команда идёт через bash. Флап на первой
попытке ssh_exec/push отдаёт решение пробе: она видит .bashrc, и наружу должен
уйти тот же код 254, что без флапа, а не 255 «сеть или вход».
"""
import os
from pathlib import Path
import tempfile
import unittest

from test_deploy import ROOT, bash
from test_ssh_refusal import PREFLIGHT

FAKE_SSH = r'''#!/bin/bash
echo call >> "$FAKE_NODE/calls"
n=$(wc -l < "$FAKE_NODE/calls")
if (( n <= ${FAKE_FLAPS:-0} )); then
  echo 'Connection reset by peer' >&2
  exit 255
fi
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) host=${1#*@}; shift; break ;; esac
done
cmd="$*"
cmd=${cmd//\/var\/lib\/vpn2-deploy/$FAKE_NODE/lock}
if [[ -z ${FAKE_BASHRC_HOST:-} || $host == "$FAKE_BASHRC_HOST" ]]; then
  case ${FAKE_BASHRC:-} in
    exit0) exit 0 ;;
    exit1) exit 1 ;;
  esac
fi
exec bash -c "$cmd"
'''
FAKE_SSHPASS = '#!/bin/bash\nshift 2\nexec ssh "$@"\n'
REASON = r'\.bashrc.*|не дош[её]л|не выполнил'


class Node:
    def __init__(self, bashrc, flaps=0, bashrc_host=''):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'bin').mkdir()
        (self.root / 'node' / 'lock').mkdir(parents=True)
        for name, text in (('ssh', FAKE_SSH), ('sshpass', FAKE_SSHPASS)):
            (self.root / 'bin' / name).write_text(text)
            (self.root / 'bin' / name).chmod(0o755)
        self.env = {'PATH': f'{self.root}/bin:{os.environ["PATH"]}',
                    'FAKE_NODE': str(self.root / 'node'), 'FAKE_BASHRC': bashrc,
                    'FAKE_FLAPS': str(flaps), 'FAKE_BASHRC_HOST': bashrc_host}

    def bash(self, body):
        exports = ''.join(f"export {k}='{v}'\n" for k, v in self.env.items())
        return bash(exports + f'''DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
sleep() {{ SECONDS=$((SECONDS + $1)); }}
SSH_USER=root SSH_HOST=entry.example SSH_PASS=fixture SSH_PROBE_TIMEOUT=5
''' + body, self.directory.name)

    def calls(self):
        path = self.root / 'node' / 'calls'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.directory.cleanup()


def verdict(stderr):
    lines = [line for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else ''


class BashrcAfterFlapTests(unittest.TestCase):
    def test_1_stage_ssh_after_flap_returns_bashrc_code(self):
        for bashrc in ('exit0', 'exit1'):
            for name, body in (('ssh_exec', 'ssh_exec true'),
                               ('push', 'echo payload > payload; push payload')):
                with self.subTest(bashrc=bashrc, call=name), Node(bashrc, flaps=1) as node:
                    result = node.bash(f'rc=0; {body} || rc=$?\necho "rc=$rc"\n')
                    self.assertIn('rc=254', result.stdout,
                                  f'{name} после флапа отдал не код .bashrc:\n{result.stderr}')
                    self.assertRegex(result.stderr, REASON)
                    self.assertNotIn('повтор 1/4', result.stderr)
                    # Флап и одна проба, без повторов.
                    self.assertEqual(node.calls(), 2, result.stderr)
                    # Причина печатается один раз.
                    self.assertEqual(result.stderr.count('.bashrc'), 1, result.stderr)

    def test_2_deploy_lock_after_flap_names_bashrc(self):
        with Node('exit1', flaps=1) as node:
            result = node.bash(f'''source {ROOT}/infra/lib/deploy-release.sh
install_prereqs() {{ :; }}
load_env() {{ HOST_TAG=$1; }}
expand_hosts() {{ :; }}
VPN2_DEVELOPMENT=true
release_start rollback
echo deploy-locked
''')
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('deploy-locked', result.stdout)
            last = verdict(result.stderr)
            self.assertIn('.bashrc', last, result.stderr)
            self.assertNotIn('unreachable', last)

    def test_3_exit_with_bashrc_exit_is_not_skipped(self):
        preflight = PREFLIGHT.replace('expand_hosts() { echo ru; }',
                                      'expand_hosts() { echo ru de; }').replace(
            'load_env() { :; }', 'load_env() { SSH_HOST=$1.example; }')
        self.assertIn('echo ru de', preflight)
        for bashrc in ('exit0', 'exit1'):
            with self.subTest(bashrc=bashrc), Node(bashrc, bashrc_host='de.example') as node:
                result = node.bash(preflight + 'echo deploy-continues\n')
                out = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, out)
                self.assertNotIn('deploy-continues', result.stdout)
                self.assertNotIn('пропущен', out, 'экзит с exit в .bashrc пропущен как недоступный')
                self.assertRegex(out, REASON)
                self.assertIn('de.example', out)
                # ru прошёл с первой пробы, de - одна проба без повторов.
                self.assertEqual(node.calls(), 2, out)


if __name__ == '__main__':
    unittest.main()
