"""Экзиты замолчали по ssh во время стадии entry - это не закрытый вход.

ssh entry→экзит - исходящее соединение entry, закрытый стадией вход его не рвёт:
экзит, который ребутится или перезапускает sshd в своей параллельной стадии,
о входе на entry ничего не говорит.
"""
import unittest
from unittest import mock

import test_fw_guard_entry_refs as refs


# После arm на entry экзиты перестают отвечать по ssh.
SILENT = r'''
eval "orig_$(declare -f net)"
net() {
  if [[ -e armed && $SSH_HOST == exit-* ]]; then
    touch down-se down-fi
  fi
  orig_net "$@"
}
'''


class EntryBeatSilentExitsTests(unittest.TestCase):
    def test_exits_silent_after_arm_do_not_roll_back_open_entry(self):
        runner = refs.EntryBeatReferenceExitTests()
        with mock.patch.object(refs, 'NET', refs.NET + SILENT):
            result, beats, probes, detail = runner.run_entry_stage(files=('outside-open',))
        self.assertTrue(probes, 'опорные экзиты не спрошены до стадии\n' + detail)
        self.assertGreater(beats.count('entry'), 1, 'пульс пропущен при открытом входе\n' + detail)
        self.assertNotIn('откатан', result.stdout, detail)
        self.assertEqual(result.stdout.count('экзиты перестали отвечать по ssh'), 1, detail)
        self.assertIn('RESULT=0', result.stdout, detail)

    def test_disarm_does_not_wait_for_silent_exits(self):
        runner = refs.EntryBeatReferenceExitTests()
        with mock.patch.object(refs, 'NET', refs.NET + SILENT):
            result, beats, probes, detail = runner.run_entry_stage(files=('outside-open',))
        self.assertNotIn('экзиты не видят вход', result.stdout, 'disarm ждал молчащих экзитов\n' + detail)
        self.assertIn('RESULT=0', result.stdout, detail)


if __name__ == '__main__':
    unittest.main()
