"""99-verify: stderr .bashrc экзита, пришедший после вывода команды, не попадает в лог.

Настоящий sshd отдаёт stdout и stderr разными каналами, и stderr .bashrc
приходит позже метки из stdout команды. Подставной ssh воспроизводит этот
порядок: сначала stdout (шум .bashrc и вывод команды), затем stderr .bashrc.
"""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_verify
from test_verify_exit_ssh_bashrc import LABEL, MOCK, REASON

FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
case ${FAKE_BASHRC:-} in
  late)    printf 'Welcome to exit node\n'; bash -c "$cmd"; rc=$?
           printf 'bashrc warning\n' >&2; exit $rc ;;
  erronly) bash -c "$cmd"; rc=$?; printf 'bash: foo: command not found\n' >&2; exit $rc ;;
  nomark)  printf 'bashrc warning\n' >&2; exit 0 ;;
  conn)    echo 'ssh: connect to host exit port 22: Connection refused' >&2; exit 255 ;;
esac
bash -c "$cmd"
'''
NOISE = ('Welcome to exit node', 'bashrc warning', 'command not found')
SSH_ERROR = 'ssh: connect to host exit port 22: Connection refused'


class VerifyExitSshStderrTests(unittest.TestCase):
    def verify(self, bashrc):
        """(вывод 99-verify, его код, строка отчёта проверки ssh экзита)."""
        with tempfile.TemporaryDirectory(prefix='verify-exit-ssh-stderr-') as directory:
            fake = Path(directory) / 'ssh'
            fake.write_text(FAKE_SSH)
            fake.chmod(0o755)
            with mock.patch.object(test_verify, 'MOCK', MOCK):
                result = test_verify.VerifyTest('run_verify').run_verify(
                    extra_env={'FAKE_SSH': str(fake), 'FAKE_BASHRC': bashrc})
        rows = [r for r in (result.report or {}).get('checks', []) if r['check'] == LABEL]
        self.assertEqual(len(rows), 1, f'нет строки отчёта «{LABEL}»:\n{result.stdout}')
        print(f'{bashrc}: exit={result.returncode} row={rows[0]["result"]}: {rows[0]["reason"]}')
        return result.stdout, result.returncode, rows[0]

    def assert_no_noise(self, out, row):
        for line in NOISE:
            self.assertNotIn(line, out, f'вывод .bashrc экзита попал в лог verify:\n{out}')
            self.assertNotIn(line, row['reason'], 'вывод .bashrc экзита попал в отчёт')

    def assert_pass(self, bashrc):
        out, code, row = self.verify(bashrc)
        self.assertIn(f'✓ {LABEL}', out)
        self.assertEqual((row['result'], code), ('Работает', 0), out)
        self.assert_no_noise(out, row)

    def test_1_bashrc_stderr_after_mark_is_not_logged(self):
        self.assert_pass('late')

    def test_2_bashrc_stderr_only_after_mark_is_not_logged(self):
        self.assert_pass('erronly')

    def test_3_no_mark_reason_is_bashrc(self):
        out, code, row = self.verify('nomark')
        self.assertIn(f'✗ {LABEL}', out)
        self.assertEqual(row['result'], 'Ошибка')
        self.assertRegex(row['reason'], REASON)
        self.assertNotEqual(code, 0)

    def test_4_unreachable_node_shows_ssh_error(self):
        out, code, row = self.verify('conn')
        self.assertIn(f'✗ {LABEL}', out)
        self.assertIn(SSH_ERROR, out)
        self.assertEqual(row['result'], 'Ошибка')
        self.assertIn('255', row['reason'])
        self.assertIn(SSH_ERROR, row['reason'])
        self.assertNotRegex(row['reason'], REASON)
        self.assertNotEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
