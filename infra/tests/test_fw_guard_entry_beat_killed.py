"""Пульс, оборванный kill перед disarm, не откатывает здоровую стадию.

deploy.sh снимает фоновый пульс kill-ом в любой его точке, в том числе посреди
записи отметки. Отметка пульса пишется целиком или не пишется: недописанная
отметка не превращается в «пульса не было с начала эпохи». Медленный раннер
CI попадал kill-ом в запись отметки сам; здесь запись растянута, чтобы kill
приходил в неё каждый раз.
"""
import unittest
from unittest import mock

import test_fw_guard_entry_refs as refs
import test_fw_guard_entry_silent as silent


# Отметка пульса пишется 5 с - дольше потолка, до которого стадия ждёт первый пульс
# (arm + 3 с): стадия кончается, и kill приходит посреди записи. Без пульса возраст
# на disarm ~3 с, поэтому окно 6 с: откат здесь означает только недописанную отметку.
WINDOW = 60
SLOW_BEAT = r'''
eval "orig_$(declare -f now_ms)"
now_ms() {
  if [[ ${FUNCNAME[1]} == *net && $cmd == *'fw-guard.sh beat'* ]]; then command sleep 5; fi
  orig_now_ms
}
'''


class EntryBeatKilledMidWriteTests(unittest.TestCase):
    def check_not_rolled_back(self, net):
        runner = refs.EntryBeatReferenceExitTests()
        with mock.patch.object(refs, 'NET', net), mock.patch.object(refs, 'WINDOW', WINDOW):
            result, _, _, detail = runner.run_entry_stage(files=('outside-open',), extra=SLOW_BEAT)
        self.assertNotIn('откатан', result.stdout, 'оборванный пульс откатил стадию\n' + detail)
        self.assertIn('RESULT=0', result.stdout, detail)

    def test_killed_beat_with_silent_exits_does_not_roll_back(self):
        self.check_not_rolled_back(refs.NET + silent.SILENT)

    def test_killed_beat_with_answering_exits_does_not_roll_back(self):
        self.check_not_rolled_back(refs.NET)


if __name__ == '__main__':
    unittest.main()
