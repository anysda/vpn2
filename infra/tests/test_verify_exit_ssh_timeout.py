"""99-verify: вход на экзит через hy2-mgmt ограничен общим временем.

ConnectTimeout ограничивает только соединение. Зависший .bashrc root экзита
(или команда, которая начала выполняться и не кончилась) не должен держать
verify без предела: проверка за SSH_PROBE_TIMEOUT секунд даёт «Ошибка» с
причиной про предел, как ssh_probe в infra/lib/ssh.sh. Подставной ssh спит
дольше предела и пишет pid свой и дочернего sleep - после выхода по пределу
их быть не должно.
"""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import test_verify
from test_verify_exit_ssh_bashrc import LABEL, MOCK, REASON

FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
echo $$ >> "$FAKE_PIDS"
hang() { sleep "$FAKE_SLEEP" & echo $! >> "$FAKE_PIDS"; wait $!; }
noise() { printf 'Welcome to exit node\n'; printf 'bashrc warning\n' >&2; }
case ${FAKE_BASHRC:-} in
  before) noise; hang ;;
  after)  noise; bash -c "$cmd"; hang; exit 0 ;;
  noise)  noise ;;
  exit1)  noise; exit 1 ;;
  conn)   echo 'ssh: connect to host exit port 22: Connection refused' >&2; exit 255 ;;
esac
bash -c "$cmd"
'''
LIMIT = 3
HANG = 9
# Причина называет предел времени и его секунды.
LIMIT_REASON = r'(?i)предел|тайм-?аут|timeout|не уложил|не ответил|не заверш|завис'
# Причина «shell не дошёл до команды» - про exit в .bashrc, не про зависание.
NO_MARK = r'не дош[её]л|не выполн'
LIMIT_SECONDS = rf'(?<!\d){LIMIT}\s*(с|сек\w*|s)(?!\w)'


class VerifyExitSshTimeoutTests(unittest.TestCase):
    def verify(self, bashrc, sleep=HANG):
        """(вывод, код, строка отчёта, секунды прогона, pid заглушки, остаток во временном каталоге)."""
        with tempfile.TemporaryDirectory(prefix='verify-exit-ssh-timeout-') as directory:
            root = Path(directory)
            fake = root / 'ssh'
            fake.write_text(FAKE_SSH)
            fake.chmod(0o755)
            pids = root / 'pids'
            tmp = root / 'tmp'
            tmp.mkdir()
            env = {'FAKE_SSH': str(fake), 'FAKE_BASHRC': bashrc, 'FAKE_SLEEP': str(sleep),
                   'FAKE_PIDS': str(pids), 'SSH_PROBE_TIMEOUT': str(LIMIT), 'TMPDIR': str(tmp)}
            started = time.monotonic()
            with mock.patch.object(test_verify, 'MOCK', MOCK):
                result = test_verify.VerifyTest('run_verify').run_verify(extra_env=env)
            elapsed = time.monotonic() - started
            pid_list = [int(p) for p in pids.read_text().split()] if pids.exists() else []
            leftovers = sorted(p.name for p in tmp.iterdir())
        rows = [r for r in (result.report or {}).get('checks', []) if r['check'] == LABEL]
        self.assertEqual(len(rows), 1, f'нет строки отчёта «{LABEL}»:\n{result.stdout}')
        print(f'{bashrc or "clean"}: exit={result.returncode} {elapsed:.1f}s '
              f'row={rows[0]["result"]}: {rows[0]["reason"]}')
        return result.stdout, result.returncode, rows[0], elapsed, pid_list, leftovers

    @staticmethod
    def alive(pids):
        live = []
        for pid in pids:
            try:
                state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                continue
            if state != 'Z':
                live.append(pid)
        return live

    def assert_limit_failure(self, bashrc):
        _, _, _, baseline, _, _ = self.verify('')
        out, code, row, elapsed, pids, leftovers = self.verify(bashrc)
        self.assertNotIn(f'✓ {LABEL}', out, f'зависший вход на экзит принят за прошедший:\n{out}')
        self.assertIn(f'✗ {LABEL}', out)
        self.assertEqual(row['result'], 'Ошибка')
        self.assertRegex(row['reason'], LIMIT_REASON, 'причина не про предел времени')
        self.assertRegex(row['reason'], LIMIT_SECONDS, f'в причине нет секунд предела ({LIMIT} с)')
        self.assertNotRegex(row['reason'], NO_MARK, 'зависание принято за exit в .bashrc')
        self.assertNotEqual(code, 0)
        self.assertLess(elapsed - baseline, LIMIT + 3,
                        f'verify ждал зависший экзит {elapsed - baseline:.1f} с сверх обычного, предел {LIMIT} с')
        return pids, leftovers

    def test_1_hang_before_mark_fails_by_limit(self):
        self.assert_limit_failure('before')

    def test_2_hang_after_mark_fails_by_limit(self):
        self.assert_limit_failure('after')

    def test_3_no_processes_or_temp_files_left_after_limit(self):
        for bashrc in ('before', 'after'):
            with self.subTest(bashrc=bashrc):
                pids, leftovers = self.assert_limit_failure(bashrc)
                self.assertGreaterEqual(len(pids), 2, 'заглушка ssh не записала pid свой и sleep')
                deadline = time.monotonic() + 2
                while self.alive(pids) and time.monotonic() < deadline:
                    time.sleep(0.1)
                live = self.alive(pids)
                for pid in live:
                    os.kill(pid, 9)
                self.assertEqual(live, [], 'заглушка ssh или её дети живут после выхода по пределу')
                self.assertEqual(leftovers, [], 'временный файл ошибок ssh не удалён')

    def test_4_without_hang_unchanged(self):
        out, code, row, _, _, leftovers = self.verify('')
        self.assertIn(f'✓ {LABEL}', out)
        self.assertEqual((row['result'], code, leftovers), ('Работает', 0, []), out)
        out, code, row, _, _, _ = self.verify('noise')
        self.assertIn(f'✓ {LABEL}', out)
        self.assertEqual((row['result'], code), ('Работает', 0), out)
        for line in ('Welcome to exit node', 'bashrc warning'):
            self.assertNotIn(line, out)
        out, code, row, _, _, _ = self.verify('exit1')
        self.assertIn(f'✗ {LABEL}', out)
        self.assertEqual(row['result'], 'Ошибка')
        self.assertRegex(row['reason'], REASON)
        self.assertNotEqual(code, 0)
        out, code, row, _, _, leftovers = self.verify('conn')
        self.assertIn(f'✗ {LABEL}', out)
        self.assertEqual(row['result'], 'Ошибка')
        self.assertIn('255', row['reason'])
        self.assertNotRegex(row['reason'], LIMIT_REASON)
        self.assertEqual(leftovers, [])
        self.assertNotEqual(code, 0)

    def test_5_bashrc_just_under_limit_passes(self):
        out, code, row, _, _, _ = self.verify('before', sleep=LIMIT - 1.5)
        self.assertIn(f'✓ {LABEL}', out)
        self.assertEqual((row['result'], code), ('Работает', 0), out)


if __name__ == '__main__':
    unittest.main()
