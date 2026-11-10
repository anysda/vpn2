"""Отказ во входе не подставляет entry под fail2ban экзита.

fail2ban экзита (00-bootstrap) считает строки `Failed password`: одна попытка
sshpass с неверным паролем - одна строка. Деплой не должен набирать maxretry
за два запуска подряд, а адреса entry стоят в ignoreip.
"""
import re
import unittest

from test_deploy import source
from test_ssh_refusal import PREFLIGHT, WRONG_PASSWORD, run

KEY_ONLY = 'root@203.0.113.7: Permission denied (publickey).\n'
RECORD_SLEEP = 'sleep() { echo "pause $1" >&2; }\n'


def maxretry():
    jail = source('infra/ansible/roles/base/templates/anysda.local.j2')
    return int(re.search(r'^maxretry = (\d+)$', jail, re.M)[1])


class SshAuthAttemptsTests(unittest.TestCase):
    def test_preflight_wrong_password_two_runs_stay_below_fail2ban_maxretry(self):
        result, calls, _ = run(WRONG_PASSWORD, 5, RECORD_SLEEP + PREFLIGHT)
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        # Человек видит причину с первой попытки.
        first = next(line for line in out.splitlines() if 'попытка 1/' in line)
        self.assertIn('нода отказала во входе', first)
        self.assertLess(2 * calls, maxretry(), out)

    def test_preflight_waits_for_late_password_once(self):
        # После переустановки ОС пароль выставляется не сразу: один повтор с паузой.
        result, calls, _ = run(WRONG_PASSWORD, 5, RECORD_SLEEP + PREFLIGHT)
        out = result.stdout + result.stderr
        self.assertEqual(calls, 2, out)
        pauses = [int(n) for n in re.findall(r'^pause (\d+)$', out, re.M)]
        self.assertEqual(len(pauses), 1, out)
        self.assertGreaterEqual(pauses[0], 20, out)
        self.assertIn('пароль мог ещё не выставиться', out)

    def test_push_stops_on_wrong_password(self):
        result, calls, _ = run(WRONG_PASSWORD, 5, 'push err')
        self.assertEqual(result.returncode, 5)
        self.assertEqual(calls, 1, result.stderr)
        self.assertIn('нода отказала во входе', result.stderr)
        self.assertNotIn('повтор 1/4', result.stderr)

    def test_stage_ssh_stops_when_key_refused(self):
        for name in ('ssh_exec true', 'push err'):
            with self.subTest(name):
                result, calls, _ = run(KEY_ONLY, 255, name)
                self.assertEqual(result.returncode, 255, result.stderr)
                self.assertIn('нода отказала во входе', result.stderr)
                self.assertNotIn('повтор 1/4', result.stderr)
                self.assertLessEqual(calls, 2, result.stderr)


if __name__ == '__main__':
    unittest.main()
