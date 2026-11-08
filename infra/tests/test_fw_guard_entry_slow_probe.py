"""Медленный экзит виден опорным и на загруженном раннере.

Время стенда ускорено в 10 раз, а запуск bash и awk в сессии пробы - нет: запас
потолка SSH_PROBE_TIMEOUT над входом LOGIN=13 сжат до 0,2 с. Запуск сессии,
замедленный на 0,3 с, не должен превращать открытый вход в «экзит не ответил».
"""
import unittest
from unittest import mock

import test_fw_guard_entry_refs as refs
import test_fw_guard_entry_slow as slow


# Сессия пробы в заглушке SLOW - bash -c с $0 = _: BASH_ENV тормозит только её.
SLOW_START = r'''
printf '[[ $0 != _ ]] || command sleep 0.3\n' > probe-start.sh
export BASH_ENV=$PWD/probe-start.sh
'''


class EntryBeatSlowProbeStartTests(unittest.TestCase):
    def test_slow_probe_start_keeps_open_entry_seen(self):
        runner = refs.EntryBeatReferenceExitTests()
        with mock.patch.object(refs, 'NET', refs.NET + 'LOGIN=13\n' + slow.SLOW + SLOW_START):
            result, beats, probes, detail = runner.run_entry_stage(exit_tags='se',
                                                                   files=('outside-open',))
        self.assertIn('entry', beats, detail)
        self.assertNotIn('не проверяется', result.stdout, detail)
        self.assertNotIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=0', result.stdout, detail)


if __name__ == '__main__':
    unittest.main()
