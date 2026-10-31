"""99-verify: код 124/137 от экзита незадолго до предела - не срабатывание предела.

Экзит честно отвечает кодом 124 или 137 (exit в .bashrc или код команды) за
предел-0,5 и предел-0,1 с. Предел ещё не истёк, значит причина та же, что на
dev, без слов про предел. Прошедшее время, измеренное целыми секундами,
набегает до предела раньше, чем он истёк: повторы ловят смену секунды.
Настоящее зависание по-прежнему даёт предел - test_verify_exit_ssh_timeout.
"""
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import test_verify
from test_verify_exit_ssh_bashrc import LABEL, MOCK

FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
case ${FAKE_BASHRC:-} in
  before) sleep "$FAKE_SLEEP"; exit "$FAKE_RC" ;;
  after)  bash -c "$cmd"; sleep "$FAKE_SLEEP"; exit "$FAKE_RC" ;;
esac
bash -c "$cmd"
'''
LIMIT = 3
DELAYS = (LIMIT - 0.5, LIMIT - 0.1)
REPEATS = 2
NO_MARK = r'не дош[её]л|не выполн'
LIMIT_REASON = r'(?i)предел|тайм-?аут|timeout|не уложил|не ответил|завис'


class VerifyExitSshLimitEdgeTests(unittest.TestCase):
    def reason(self, where, rc, sleep):
        """Причина строки «ssh exit через hy2-mgmt» и код 99-verify."""
        with tempfile.TemporaryDirectory(prefix='verify-exit-ssh-edge-') as directory:
            fake = Path(directory) / 'ssh'
            fake.write_text(FAKE_SSH)
            fake.chmod(0o755)
            with mock.patch.object(test_verify, 'MOCK', MOCK):
                result = test_verify.VerifyTest('run_verify').run_verify(extra_env={
                    'FAKE_SSH': str(fake), 'FAKE_BASHRC': where, 'FAKE_RC': str(rc),
                    'FAKE_SLEEP': str(sleep), 'SSH_PROBE_TIMEOUT': str(LIMIT)})
        rows = [r for r in (result.report or {}).get('checks', []) if r['check'] == LABEL]
        self.assertEqual(len(rows), 1, f'нет строки отчёта «{LABEL}»:\n{result.stdout}')
        self.assertEqual(rows[0]['result'], 'Ошибка', result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return rows[0]['reason']

    def test_1_exit_code_just_before_limit_is_not_limit(self):
        wrong = []
        for where, expected in (('before', NO_MARK), ('after', None)):
            for rc in (124, 137):
                for sleep in DELAYS:
                    hits = 0
                    for _ in range(REPEATS):
                        reason = self.reason(where, rc, sleep)
                        pattern = expected or rf'(?<!\d){rc}(?!\d)'
                        bad = bool(re.search(LIMIT_REASON, reason)) or not re.search(pattern, reason)
                        hits += bad
                        print(f'{where}{rc} через {sleep} с: {"ПРЕДЕЛ" if bad else "ок"}: {reason}')
                    if hits:
                        wrong.append(f'{where} {rc} через {sleep} с: {hits} из {REPEATS} - {reason}')
        self.assertEqual(wrong, [], 'ответ экзита кодом 124/137 до истечения предела выдан за предел:\n'
                         + '\n'.join(wrong))


if __name__ == '__main__':
    unittest.main()
