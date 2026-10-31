"""Сбой входа на entry после перекатки порта - итог называет перекаченный экзит и порт.

Подмена ssh та же, что в test_verify_rotate_ssh_failure: настоящие
verify_and_rotate_ports, _clash_delay и ssh_exec, подставные только ssh узлов и
стадии. Перекатка уже записала порт в config.yaml и раскатила экзит, отката нет,
поэтому «порты не менял» в итоге - неправда.
"""
import unittest

import test_verify_rotate_ssh_failure as base
from test_verify_rotate_ssh_failure import REASONS, Node, fatal, stages

UNTOUCHED = 'экзиты и порты не менял'


class VerifyRotateSshRecheckTests(unittest.TestCase):
    show = base.VerifyRotateSshFailureTests.show
    assert_entry_login_failure = base.VerifyRotateSshFailureTests.assert_entry_login_failure

    def test_entry_refuses_on_recheck_names_rotated_port(self):
        """Край 1: отказ входа на повторной проверке - итог называет alpha и 4443, не «порты не менял»."""
        for kind in REASONS:
            with self.subTest(kind=kind), Node() as node:
                # Первый curl - туннель alpha мёртв, второй (после перекатки на 4443) - отказ входа.
                result = node.run(fail=f'ru:delay:2:{kind}', tunnel='alpha:timeout')
                self.show(f'entry recheck {kind}', result)
                self.assert_entry_login_failure(result, kind)
                last = fatal(result.stdout)
                self.assertNotIn('порты не менял', last, result.stdout)
                self.assertIn('alpha', last, f'итог не называет перекаченный экзит: {last!r}')
                self.assertIn('4443', last, f'итог не называет перекаченный порт: {last!r}')
                # Перекатка ровно одна, отката на прежний порт нет.
                ports = [call for call in result.calls if call.startswith('config-set-port.py')]
                self.assertEqual(ports, ['config-set-port.py alpha 4443'], result.stdout)
                self.assertEqual(sum('10-foreign' in call for call in result.calls), 1, result.stdout)

    def test_entry_refuses_before_rotation_keeps_text(self):
        """Край 2: отказ входа до перекатки (секрет, curl, проба) - прежнее «экзиты и порты не менял»."""
        cases = (('ru:secret:1', ''), ('ru:delay:1', ''), ('ru:probe:1', 'alpha:timeout'))
        for fail, tunnel in cases:
            for kind in REASONS:
                with self.subTest(fail=fail, kind=kind), Node() as node:
                    result = node.run(fail=f'{fail}:{kind}', tunnel=tunnel)
                    self.show(f'entry {fail} {kind}', result)
                    self.assert_entry_login_failure(result, kind)
                    self.assertIn(UNTOUCHED, fatal(result.stdout), result.stdout)
                    self.assertEqual(stages(result), [], result.stdout)


if __name__ == '__main__':
    unittest.main()
