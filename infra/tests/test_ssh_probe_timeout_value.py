"""SSH_PROBE_TIMEOUT - только целое число секунд больше нуля.

Предел идёт в timeout и в арифметику: ssh_probe и ssh_fresh (infra/lib/ssh.sh),
exit_ssh (99-verify), бюджет попыток pre-flight (_preflight_host в deploy.sh).
«0» снимает предел у timeout, «1.5» и «30s» ломают арифметику bash, «abc»
роняет timeout в rc=1 на исправном узле. Любое значение не целое > 0, и
заданное пустым тоже, - громкая ошибка с именем переменной, ненулевой код,
ssh не запускается. Не задано - 30 с, как прежде; 15 из fw_guard_exit_sees_entry
работает.
"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_verify
from test_ssh_limit_remote_code import FAKE_SSH, Node
from test_ssh_refusal import PREFLIGHT
from test_verify_exit_ssh_bashrc import LABEL, MOCK

BAD = ('0', '1.5', '30s', 'abc', '-5', '')
# Сообщения bash об арифметике и timeout о неверном пределе - не та ошибка.
SYNTAX = (r'syntax error|operand expected|value too great|error token|invalid time interval'
          r'|invalid (number|duration)|timeout --help|ошибка синтаксиса')
# Паузы pre-flight не ждём, но часы цикла они двигают, как настоящие.
FAST_SLEEP = 'sleep() { SECONDS=$((SECONDS + ${1%.*})); }\n'
SLOW = 4


def preflight(node, limit, budget=None):
    body = FAST_SLEEP + (f'PREFLIGHT_BUDGET={budget}\n' if budget else '') + PREFLIGHT
    return node.run(body, limit)


def verify(bashrc, limit, sleep=0):
    """(вывод 99-verify, его код, строка отчёта или None, вызовов ssh)."""
    with tempfile.TemporaryDirectory(prefix='ssh-probe-timeout-') as directory:
        root = Path(directory)
        (root / 'node').mkdir()
        fake = root / 'ssh'
        fake.write_text(FAKE_SSH)
        fake.chmod(0o755)
        env = {'FAKE_SSH': str(fake), 'FAKE_BASHRC': bashrc, 'FAKE_SLEEP': str(sleep),
               'FAKE_NODE': str(root / 'node'), 'LC_ALL': 'C.UTF-8'}
        environ = {k: v for k, v in os.environ.items() if k != 'SSH_PROBE_TIMEOUT'}
        if limit is not None:
            env['SSH_PROBE_TIMEOUT'] = limit
        with mock.patch.object(test_verify, 'MOCK', MOCK), mock.patch.dict(os.environ, environ, clear=True):
            result = test_verify.VerifyTest('run_verify').run_verify(extra_env=env)
        calls = root / 'node' / 'calls'
        calls = len(calls.read_text().splitlines()) if calls.exists() else 0
    rows = [r for r in (result.report or {}).get('checks', []) if r['check'] == LABEL]
    return result.stdout, result.returncode, rows[0] if rows else None, calls


class SshProbeTimeoutValueTests(unittest.TestCase):
    def assert_loud(self, what, code, out, calls):
        print(f'{what}: rc={code} calls={calls} {out.strip().splitlines()[-1:]!r}')
        self.assertNotRegex(out, SYNTAX, f'{what}: ошибка bash или timeout вместо понятной:\n{out}')
        self.assertIn('SSH_PROBE_TIMEOUT', out, f'{what}: ошибка не называет переменную:\n{out}')
        self.assertNotEqual(code, 0, f'{what}: неверный предел принят:\n{out}')
        self.assertEqual(calls, 0, f'{what}: ssh запущен с неверным пределом')

    def test_1_ssh_probe_and_ssh_fresh_reject_bad_value(self):
        for value in BAD:
            for call in (Node.probe, Node.fresh):
                with self.subTest(value=value, call=call.__name__), Node('') as node:
                    code, out, err, _ = call(node, value)
                    self.assert_loud(f'{call.__name__} SSH_PROBE_TIMEOUT={value!r}', code, out + err, node.calls())

    def test_2_preflight_rejects_bad_value(self):
        for value in BAD:
            with self.subTest(value=value), Node('') as node:
                code, out, err, _ = preflight(node, value)
                out += err
                self.assertNotIn('✓', out, f'pre-flight принял ноду при SSH_PROBE_TIMEOUT={value!r}')
                self.assert_loud(f'pre-flight SSH_PROBE_TIMEOUT={value!r}', code, out, node.calls())

    def test_3_exit_ssh_rejects_bad_value(self):
        for value in BAD:
            with self.subTest(value=value):
                out, code, row, calls = verify('', value)
                self.assertNotIn(f'✓ {LABEL}', out)
                if row:
                    self.assertEqual(row['result'], 'Ошибка')
                self.assert_loud(f'exit_ssh SSH_PROBE_TIMEOUT={value!r}', code, out, calls)

    def test_4_unset_is_30_and_15_works(self):
        for limit in (None, '15'):
            for call in (Node.probe, Node.fresh):
                with self.subTest(limit=limit, call=call.__name__), Node('slow', SLOW) as node:
                    code, out, err, elapsed = call(node, limit)
                    self.assertEqual(code, 0, f'узел ответил за {elapsed:.1f} с, предел {limit or "по умолчанию"}: '
                                              f'{out + err}')
            with self.subTest(limit=limit, call='exit_ssh'):
                out, code, row, _ = verify('slow', limit, SLOW)
                self.assertIn(f'✓ {LABEL}', out)
                self.assertEqual(row['result'], 'Работает', out)
        # Бюджет попыток pre-flight считает с пределом 30 с без переменной и 15 с ней.
        for limit, attempts in ((None, 3), ('15', 4)):
            with self.subTest(limit=limit, call='pre-flight'), Node('before255') as node:
                code, out, err, _ = preflight(node, limit, budget=40)
                self.assertNotEqual(code, 0)
                self.assertIn(f'попытка 1/{attempts}:', out + err)
                self.assertNotRegex(out + err, SYNTAX)


if __name__ == '__main__':
    unittest.main()
