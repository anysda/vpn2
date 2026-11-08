"""Медленный экзит виден опорным и на загруженном раннере.

Время стенда ускорено в 10 раз, а запуск bash и awk в сессии пробы - нет: запас
потолка SSH_PROBE_TIMEOUT над входом LOGIN=13 сжат до 0,2 с. Запуск сессии,
замедленный на 0,3 с, не должен превращать открытый вход в «экзит не ответил».
Запуск, замедленный на 1 с, не должен превращать закрытый вход в «экзит не
ответил»: запасной обрыв заглушки не обгоняет её потолок.
"""
import unittest
from unittest import mock

import test_fw_guard_entry_refs as refs
import test_fw_guard_entry_slow as slow


# Сессия пробы в заглушке SLOW - bash -c с $0 = _: BASH_ENV тормозит только её.
def slow_start(delay):
    return (f"printf '[[ $0 != _ ]] || command sleep {delay}\\n' > probe-start.sh\n"
            'export BASH_ENV=$PWD/probe-start.sh\n')


class EntryBeatSlowProbeStartTests(unittest.TestCase):
    def run_slow_start(self, login, delay, files):
        runner = refs.EntryBeatReferenceExitTests()
        with mock.patch.object(refs, 'NET', refs.NET + f'LOGIN={login}\n' + slow.SLOW + slow_start(delay)), \
                mock.patch.object(refs, 'WINDOW', slow.WINDOW):
            return runner.run_entry_stage(exit_tags='se', files=files)

    def test_slow_probe_start_keeps_open_entry_seen(self):
        result, beats, probes, detail = self.run_slow_start(13, 0.3, ('outside-open',))
        self.assertIn('entry', beats, detail)
        self.assertNotIn('не проверяется', result.stdout, detail)
        self.assertNotIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=0', result.stdout, detail)

    def test_slow_probe_start_keeps_closed_entry_answered(self):
        result, beats, probes, detail = self.run_slow_start(8, 1, ('outside-open', 'stage-closes'))
        self.assertTrue(probes, 'экзит не спрошен до стадии\n' + detail)
        self.assertNotIn('entry-closed', beats, 'пульс через lo при закрытом стадией входе\n' + detail)
        self.assertIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=1', result.stdout, detail)


if __name__ == '__main__':
    unittest.main()
