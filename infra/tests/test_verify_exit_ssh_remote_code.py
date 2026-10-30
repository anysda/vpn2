"""99-verify: код 124/137 с экзита - код удалённой стороны, а не срабатывание предела.

ssh возвращает код команды на узле. Мгновенный exit 124 или exit 137 в .bashrc
root экзита (или exec timeout ...) - это тот же exit в .bashrc, что и прежде:
причина «shell узла не дошёл до команды», без слов про предел и без ожидания
предела. Предел - только когда вход действительно шёл не меньше
SSH_PROBE_TIMEOUT секунд (зависание - test_verify_exit_ssh_timeout).
"""
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import test_verify
from test_verify_exit_ssh_bashrc import LABEL, MOCK

FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
noise() { printf 'Welcome to exit node\nmotd line\n'; printf 'bashrc warning\n' >&2; }
case ${FAKE_BASHRC:-} in
  before124) noise; exit 124 ;;
  before137) noise; exit 137 ;;
  after124)  bash -c "$cmd"; exit 124 ;;
  after137)  bash -c "$cmd"; exit 137 ;;
  noise)     noise ;;
  conn)      echo 'ssh: connect to host exit port 22: Connection refused' >&2; exit 255 ;;
esac
bash -c "$cmd"
'''
LIMIT = 3
NOISE = ('Welcome to exit node', 'motd line', 'bashrc warning')
NO_MARK = r'не дош[её]л|не выполн'
LIMIT_REASON = r'(?i)предел|тайм-?аут|timeout|не уложил|не ответил|завис'


class VerifyExitSshRemoteCodeTests(unittest.TestCase):
    def verify(self, bashrc):
        """(вывод 99-verify, его код, строка отчёта, секунды прогона)."""
        with tempfile.TemporaryDirectory(prefix='verify-exit-ssh-code-') as directory:
            fake = Path(directory) / 'ssh'
            fake.write_text(FAKE_SSH)
            fake.chmod(0o755)
            started = time.monotonic()
            with mock.patch.object(test_verify, 'MOCK', MOCK):
                result = test_verify.VerifyTest('run_verify').run_verify(
                    extra_env={'FAKE_SSH': str(fake), 'FAKE_BASHRC': bashrc,
                               'SSH_PROBE_TIMEOUT': str(LIMIT)})
            elapsed = time.monotonic() - started
        rows = [r for r in (result.report or {}).get('checks', []) if r['check'] == LABEL]
        self.assertEqual(len(rows), 1, f'нет строки отчёта «{LABEL}»:\n{result.stdout}')
        print(f'{bashrc or "clean"}: exit={result.returncode} {elapsed:.1f}s '
              f'row={rows[0]["result"]}: {rows[0]["reason"]}')
        return result.stdout, result.returncode, rows[0], elapsed

    def assert_remote_code_failure(self, bashrc, reason):
        _, _, _, baseline = self.verify('')
        out, code, row, elapsed = self.verify(bashrc)
        self.assertNotIn(f'✓ {LABEL}', out, out)
        self.assertIn(f'✗ {LABEL}', out)
        self.assertEqual(row['result'], 'Ошибка')
        self.assertRegex(row['reason'], reason)
        self.assertNotRegex(row['reason'], LIMIT_REASON,
                            'код с экзита принят за срабатывание предела времени')
        self.assertNotEqual(code, 0)
        self.assertLess(elapsed - baseline, LIMIT - 1, 'verify ждал предел при мгновенном отказе экзита')
        for line in NOISE:
            self.assertNotIn(line, out, f'вывод .bashrc экзита попал в лог verify:\n{out}')

    def test_1_bashrc_exit_124_137_before_mark_is_bashrc_exit(self):
        for rc in (124, 137):
            with self.subTest(rc=rc):
                self.assert_remote_code_failure(f'before{rc}', NO_MARK)

    def test_2_command_exit_124_137_after_mark_is_remote_code(self):
        for rc in (124, 137):
            with self.subTest(rc=rc):
                self.assert_remote_code_failure(f'after{rc}', rf'(?<!\d){rc}(?!\d)')

    def test_3_pass_noise_and_unreachable_unchanged(self):
        for bashrc in ('', 'noise'):
            with self.subTest(bashrc=bashrc):
                out, code, row, _ = self.verify(bashrc)
                self.assertIn(f'✓ {LABEL}', out)
                self.assertEqual((row['result'], code), ('Работает', 0), out)
                for line in NOISE:
                    self.assertNotIn(line, out)
        out, code, row, _ = self.verify('conn')
        self.assertIn(f'✗ {LABEL}', out)
        self.assertEqual(row['result'], 'Ошибка')
        self.assertIn('255', row['reason'])
        self.assertIn('Connection refused', row['reason'])
        self.assertNotRegex(row['reason'], LIMIT_REASON)
        self.assertNotEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
