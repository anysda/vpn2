"""99-verify: вход на экзит через hy2-mgmt не пропускает вывод .bashrc узла в лог.

Настоящий 99-verify на ru с заглушками из test_verify; ssh подменён на
подставной sshd с login shell: сначала .bashrc экзита (режим из FAKE_BASHRC),
затем команда через bash. Проверка должна отрезать вывод до метки начала, как
ssh_exec и push в infra/lib/ssh.sh, и отличать exit в .bashrc от прошедшей
проверки.
"""
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import test_verify

FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
noise() { printf 'Welcome to exit node\nmotd line\n'; printf 'bashrc warning\n' >&2; }
case ${FAKE_BASHRC:-} in
  noise) noise ;;
  exit0) exit 0 ;;
  exit1) noise; exit 1 ;;
  conn)  echo 'ssh: connect to host exit port 22: Connection refused' >&2; exit 255 ;;
esac
bash -c "$cmd"
'''
# Остальные команды узла - прежние заглушки test_verify, ssh - подставной sshd.
MOCK = test_verify.MOCK.replace(
    "fault = os.environ['FAULT']\n",
    "if name == 'ssh' and os.environ.get('FAKE_SSH'):\n"
    "    os.execv(os.environ['FAKE_SSH'], [os.environ['FAKE_SSH']] + sys.argv[1:])\n"
    "fault = os.environ['FAULT']\n", 1)
NOISE = ('Welcome to exit node', 'motd line', 'bashrc warning')
REASON = r'\.bashrc|не дош[её]л|не выполн'
LABEL = 'ssh exit через hy2-mgmt'


class VerifyExitSshBashrcTests(unittest.TestCase):
    def verify(self, bashrc):
        """(вывод 99-verify, его код, строка отчёта проверки ssh экзита)."""
        assert MOCK != test_verify.MOCK
        with tempfile.TemporaryDirectory(prefix='verify-exit-ssh-') as directory:
            fake = Path(directory) / 'ssh'
            fake.write_text(FAKE_SSH)
            fake.chmod(0o755)
            with mock.patch.object(test_verify, 'MOCK', MOCK):
                result = test_verify.VerifyTest('run_verify').run_verify(
                    extra_env={'FAKE_SSH': str(fake), 'FAKE_BASHRC': bashrc})
        rows = [r for r in (result.report or {}).get('checks', []) if r['check'] == LABEL]
        self.assertEqual(len(rows), 1, f'нет строки отчёта «{LABEL}»:\n{result.stdout}')
        print(f'{bashrc or "clean"}: exit={result.returncode} row={rows[0]["result"]}: {rows[0]["reason"]}')
        return result.stdout, result.returncode, rows[0]

    def assert_no_noise(self, out):
        for line in NOISE:
            self.assertNotIn(line, out, f'вывод .bashrc экзита попал в лог verify:\n{out}')

    def assert_bashrc_failure(self, bashrc):
        out, code, row = self.verify(bashrc)
        self.assertNotIn(f'✓ {LABEL}', out, f'exit в .bashrc принят за вход на экзит:\n{out}')
        self.assertIn(f'✗ {LABEL}', out)
        self.assertEqual(row['result'], 'Ошибка')
        self.assertRegex(row['reason'], REASON, 'причина не про .bashrc')
        self.assertNotEqual(code, 0)
        self.assert_no_noise(out)

    def test_1_noisy_bashrc_is_cut_before_mark(self):
        out, code, row = self.verify('noise')
        self.assertIn(f'✓ {LABEL}', out)
        self.assertEqual(row['result'], 'Работает')
        self.assert_no_noise(out)
        self.assertEqual(code, 0, out)

    def test_2_bashrc_exit_0_is_not_a_pass(self):
        self.assert_bashrc_failure('exit0')

    def test_3_bashrc_exit_1_reason_is_bashrc(self):
        self.assert_bashrc_failure('exit1')

    def test_4_clean_node_and_unreachable_node_unchanged(self):
        out, code, row = self.verify('')
        self.assertIn(f'✓ {LABEL}', out)
        self.assertEqual((row['result'], code), ('Работает', 0), out)
        out, code, row = self.verify('conn')
        self.assertIn(f'✗ {LABEL}', out)
        self.assertEqual(row['result'], 'Ошибка')
        self.assertIn('255', row['reason'])
        self.assertNotRegex(row['reason'], REASON)
        self.assertNotEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
