"""Отказ ssh на entry при взятии замка deploy называется своей причиной.

Подставной ssh либо отказывает так, как отказывает OpenSSH (код 255 и его
текст в stderr), либо выполняет команду через bash с каталогом замка во
временном каталоге. Тест смотрит на stderr deploy - то, что видит оператор.
"""
import os
from pathlib import Path
import tempfile
import unittest

from test_deploy import ROOT, bash

FAKE_SSH = r'''#!/bin/bash
if [[ -n ${FAKE_SSH_ERROR:-} ]]; then
  printf '%s\n' "$FAKE_SSH_ERROR" >&2
  exit 255
fi
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
cmd=${cmd//\/var\/lib\/vpn2-deploy/$FAKE_NODE/lock}
exec bash -c "$cmd"
'''
FAKE_SSHPASS = '#!/bin/bash\nshift 2\nexec ssh "$@"\n'

AUTH_ERROR = 'root@entry.example: Permission denied (publickey,password).'
NET_ERRORS = ('ssh: connect to host entry.example port 22: Connection refused',
              'ssh: connect to host entry.example port 22: Connection timed out')
AUTH_WORDS = ('login', 'auth', 'permission denied', 'refused', 'вход', 'отказ', 'парол')
NET_WORDS = ('connect', 'unreachable', 'reach', 'timed out', 'refused',
             'недоступ', 'подключ', 'соединен', 'сеть')

START = f'''source {ROOT}/infra/lib/deploy-release.sh
install_prereqs() {{ :; }}
load_env() {{ HOST_TAG=$1; }}
expand_hosts() {{ :; }}
VPN2_DEVELOPMENT=true
'''


class Entry:
    def __init__(self, ssh_error=''):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'bin').mkdir()
        (self.root / 'node' / 'lock').mkdir(parents=True)
        for name, text in (('ssh', FAKE_SSH), ('sshpass', FAKE_SSHPASS)):
            (self.root / 'bin' / name).write_text(text)
            (self.root / 'bin' / name).chmod(0o755)
        self.env = {'PATH': f'{self.root}/bin:{os.environ["PATH"]}',
                    'FAKE_NODE': str(self.root / 'node'), 'FAKE_SSH_ERROR': ssh_error}

    def deploy(self, body):
        exports = ''.join(f"export {k}='{v}'\n" for k, v in self.env.items())
        return bash(exports + f'''DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_USER=root SSH_HOST=entry.example SSH_PASS=fixture SSH_PROBE_TIMEOUT=5
''' + START + body, self.directory.name)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.directory.cleanup()


def verdict(stderr):
    """Последняя строка stderr - итог deploy, который оператор читает первым."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else ''


class DeployLockSshFailureTests(unittest.TestCase):
    def assert_names_cause(self, result, ssh_error, words):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('deploy-locked', result.stdout)
        self.assertNotIn('already running', result.stderr)
        # Исходный текст ssh остаётся виден оператору.
        self.assertIn(ssh_error, result.stderr)
        last = verdict(result.stderr).lower()
        self.assertIn('entry', last, result.stderr)
        self.assertTrue(any(word in last for word in words),
                        f'итог deploy не называет причину: {last!r}\n{result.stderr}')

    def test_login_refused_on_entry_is_not_reported_as_running_deploy(self):
        with Entry(AUTH_ERROR) as entry:
            result = entry.deploy('release_start rollback\necho deploy-locked\n')
            self.assert_names_cause(result, AUTH_ERROR, AUTH_WORDS)

    def test_unreachable_entry_is_not_reported_as_running_deploy(self):
        for error in NET_ERRORS:
            with self.subTest(error=error), Entry(error) as entry:
                result = entry.deploy('release_start rollback\necho deploy-locked\n')
                self.assert_names_cause(result, error, NET_WORDS)

    def test_lock_held_by_another_deploy_still_reports_running_deploy(self):
        with Entry() as entry:
            # Второй deploy держит тот же файл замка на entry.
            result = entry.deploy('''exec 9>"$FAKE_NODE/lock/deploy.lock"
flock -n 9 || { echo cannot-hold-lock >&2; exit 2; }
release_start rollback
echo deploy-locked
''')
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('cannot-hold-lock', result.stderr)
            self.assertNotIn('deploy-locked', result.stdout)
            self.assertIn('deploy already running', verdict(result.stderr), result.stderr)

    def test_free_lock_lets_deploy_continue(self):
        with Entry() as entry:
            result = entry.deploy('''release_start rollback
test -e "$FAKE_NODE/lock/deploy.lock"
echo deploy-locked
''')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('deploy-locked', result.stdout)
            self.assertNotIn('FATAL', result.stderr)


if __name__ == '__main__':
    unittest.main()
