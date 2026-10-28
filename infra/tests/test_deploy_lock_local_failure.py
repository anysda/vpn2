"""Сбой при взятии замка deploy до flock на entry не выдаётся за идущий deploy.

Края ревью: замок берётся в coproc, и всё, что там падает до flock, - нет
envs/all.env или envs/ru.env, пустой SSH_PASS, отказ sshpass по паролю или
host-key, - завершает coproc своим кодом. Тест смотрит на stderr deploy: в нём
нет «already running», и причина сбоя видна оператору.
"""
import unittest

from test_deploy import ROOT, bash
from test_deploy_lock_ssh_failure import Entry, verdict

# sshpass отказывает до ssh: 5 - неверный пароль, 6 - неизвестный host-key,
# 7 - сменившийся host-key.
FAKE_SSHPASS = r'''#!/bin/bash
if [[ -n ${FAKE_SSHPASS_RC:-} ]]; then
  printf 'sshpass-refused-%s\n' "$FAKE_SSHPASS_RC" >&2
  exit "$FAKE_SSHPASS_RC"
fi
shift 2
exec ssh "$@"
'''

# Настоящий load_env из ssh.sh: края (а)-(в) живут в нём.
START = f'''source {ROOT}/infra/lib/deploy-release.sh
install_prereqs() {{ :; }}
expand_hosts() {{ :; }}
VPN2_DEVELOPMENT=true
'''
STUB_ENV = 'load_env() { HOST_TAG=$1; }\n'
BODY = 'release_start rollback\necho deploy-locked\n'
RU_ENV = 'SSH_USER=root\nSSH_HOST=entry.example\nSSH_PASS={}\n'


class LocalEntry(Entry):
    def __init__(self, files=(), sshpass_rc=''):
        super().__init__()
        (self.root / 'bin' / 'sshpass').write_text(FAKE_SSHPASS)
        self.env['FAKE_SSHPASS_RC'] = sshpass_rc
        envs = self.root / 'envs'
        envs.mkdir()
        for name, text in files:
            (envs / name).write_text(text)

    def run(self, prelude=''):
        exports = ''.join(f"export {k}='{v}'\n" for k, v in self.env.items())
        return bash(exports + f'''DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_PROBE_TIMEOUT=5
''' + START + prelude + BODY, self.directory.name)


class DeployLockLocalFailureTests(unittest.TestCase):
    def assert_not_running(self, result, reason):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('deploy-locked', result.stdout)
        self.assertNotIn('already running', result.stderr)
        self.assertIn(reason, result.stderr)
        self.assertIn('lock', verdict(result.stderr).lower(), result.stderr)

    def test_missing_all_env_is_not_reported_as_running_deploy(self):
        with LocalEntry() as entry:
            self.assert_not_running(entry.run(), 'all.env')

    def test_missing_ru_env_is_not_reported_as_running_deploy(self):
        with LocalEntry([('all.env', '')]) as entry:
            self.assert_not_running(entry.run(), "no env for host 'ru'")

    def test_empty_ssh_pass_is_not_reported_as_running_deploy(self):
        with LocalEntry([('all.env', ''), ('ru.env', RU_ENV.format(''))]) as entry:
            self.assert_not_running(entry.run(), 'SSH_PASS')

    def test_sshpass_refusals_are_not_reported_as_running_deploy(self):
        for rc, words in (('5', ('password', 'парол')),
                          ('6', ('host key', 'host-key')),
                          ('7', ('host key', 'host-key'))):
            with self.subTest(sshpass=rc), LocalEntry(sshpass_rc=rc) as entry:
                result = entry.run(STUB_ENV + 'SSH_USER=root SSH_HOST=entry.example SSH_PASS=fixture\n')
                self.assert_not_running(result, f'sshpass-refused-{rc}')
                last = verdict(result.stderr).lower()
                self.assertIn('entry', last, result.stderr)
                self.assertTrue(any(word in last for word in words),
                                f'итог deploy не называет причину: {last!r}\n{result.stderr}')

    def test_lock_held_still_reports_running_deploy(self):
        with LocalEntry([('all.env', ''), ('ru.env', RU_ENV.format('fixture'))]) as entry:
            result = entry.run('''exec 9>"$FAKE_NODE/lock/deploy.lock"
flock -n 9 || { echo cannot-hold-lock >&2; exit 2; }
''')
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('cannot-hold-lock', result.stderr)
            self.assertNotIn('deploy-locked', result.stdout)
            self.assertIn('deploy already running', verdict(result.stderr), result.stderr)


if __name__ == '__main__':
    unittest.main()
