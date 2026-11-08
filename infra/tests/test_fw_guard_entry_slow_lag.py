"""Пульс медленного экзита доходит до конца стадии и на загруженном раннере.

Под нагрузкой CI путь от вызова пульса до записи в beats растягивается, а kill
пульса на конце стадии гасит его посреди пути. Задержка пульса в полсекунды
(втрое больше замеренной под нагрузкой) не должна оставлять стадию без пульса.
"""
import unittest
from unittest import mock

import test_fw_guard_entry_refs as refs
import test_fw_guard_entry_slow as slow


LAG = r'''
eval "orig_lag_$(declare -f net)"
net() {
  [[ $SSH_HOST != entry-node || $* != *'fw-guard.sh beat'* ]] || command sleep 0.5
  orig_lag_net "$@"
}
'''


class EntryBeatSlowExitLagTests(unittest.TestCase):
    def test_late_beat_of_slow_exit_is_counted_before_stage_ends(self):
        runner = refs.EntryBeatReferenceExitTests()
        with mock.patch.object(refs, 'NET', refs.NET + 'LOGIN=13\n' + slow.SLOW + LAG):
            result, beats, probes, detail = runner.run_entry_stage(exit_tags='se',
                                                                   files=('outside-open',))
        self.assertIn('entry', beats, detail)
        self.assertNotIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=0', result.stdout, detail)


if __name__ == '__main__':
    unittest.main()
