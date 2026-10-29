"""Код 5 от sshpass при взятии замка deploy не значит только неверный пароль.

sshpass отдаёт 5 и тогда, когда пароль верный, а sshd на entry не пускает
root (DenyUsers, AllowUsers, Match). Итог deploy должен назвать обе причины,
иначе оператор идёт менять верный пароль. Остальные коды связки - без изменений.
"""
import unittest

from test_deploy_lock_ssh_failure import Entry, verdict

FAKE_SSHPASS = '''#!/bin/bash
if [[ -n ${FAKE_SSHPASS_EXIT:-} ]]; then
  exit "$FAKE_SSHPASS_EXIT"
fi
shift 2
exec ssh "$@"
'''
SSHD_WORDS = ('sshd', 'denyusers', 'allowusers')


class SshpassEntry(Entry):
    def __init__(self, sshpass_exit='', ssh_error=''):
        super().__init__(ssh_error)
        (self.root / 'bin' / 'sshpass').write_text(FAKE_SSHPASS)
        self.env['FAKE_SSHPASS_EXIT'] = sshpass_exit


def take_lock(entry):
    return entry.deploy('release_start rollback\necho deploy-locked\n')


class DeployLockSshpassRefusedTests(unittest.TestCase):
    def assert_failed(self, result):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('deploy-locked', result.stdout)
        return verdict(result.stderr).lower()

    def test_sshpass_5_names_password_and_sshd_refusing_root(self):
        with SshpassEntry('5') as entry:
            last = self.assert_failed(take_lock(entry))
        self.assertNotIn('already running', last)
        self.assertIn('password', last)
        self.assertTrue(any(word in last for word in SSHD_WORDS),
                        f'итог при коде 5 не называет отказ sshd пускать root: {last!r}')
        self.assertNotIn('(wrong password)', last)

    def test_ssh_255_still_points_to_ssh_reason(self):
        error = 'root@entry.example: Permission denied (publickey,password).'
        with SshpassEntry(ssh_error=error) as entry:
            last = self.assert_failed(take_lock(entry))
        self.assertIn('ssh', last)
        self.assertIn('see the ssh reason above', last)

    def test_sshpass_6_and_7_still_report_host_key(self):
        for code in ('6', '7'):
            with self.subTest(code=code), SshpassEntry(code) as entry:
                last = self.assert_failed(take_lock(entry))
                self.assertIn('host key', last)

    def test_lock_held_still_reports_running_deploy(self):
        with SshpassEntry() as entry:
            last = self.assert_failed(entry.deploy('''exec 9>"$FAKE_NODE/lock/deploy.lock"
flock -n 9 || { echo cannot-hold-lock >&2; exit 2; }
release_start rollback
echo deploy-locked
'''))
        self.assertIn('deploy lock is held', last)


if __name__ == '__main__':
    unittest.main()
