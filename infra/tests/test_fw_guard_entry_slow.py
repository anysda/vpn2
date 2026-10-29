"""Медленный экзит и потерянная проба до стадии не пропускают закрытый вход.

Экзит входит по ssh дольше прежних 5 с, а закрытый отбросом вход держит
TCP-пробу до её таймаута: экзит ответил, что входа не видит, хотя сессия к нему
и не уложилась. Одна потерянная проба до стадии не оставляет её без опорных.
Время стенда ускорено в 10 раз, как в test_fw_guard_entry_refs.
"""
import unittest
from unittest import mock

import test_fw_guard_entry_refs as refs


# Вход по ssh на экзит длится LOGIN с (в секундах стенда), потом команда пробы.
# Сессию обрывает потолок SSH_PROBE_TIMEOUT, как timeout в ssh_fresh.
SLOW = r'''
eval "orig_$(declare -f net)"
net() {
  if [[ $SSH_HOST == exit-* && $* == */dev/tcp/* ]]; then
    local node=${SSH_HOST#exit-} limit
    echo "$node $*" >> calls
    echo "$node" >> probes
    limit=$(awk -v s="${SSH_PROBE_TIMEOUT:-30}" 'BEGIN { print s / 10 }')
    command timeout "$limit" bash -c '
      command sleep "$(awk -v s="$1" "BEGIN { print s / 10 }")"
      # Вход закрыт отбросом: TCP-проба висит до своего таймаута.
      timeout() {
        [[ -e outside-open ]] && return 0
        command sleep "$(awk -v s="$1" "BEGIN { print s / 10 }")"
        return 124
      }
      eval "$2"' _ "$LOGIN" "$*"
    return
  fi
  orig_net "$@"
}
'''

# До arm первая TCP-проба с экзита теряется, следующие проходят.
LOST_BASELINE = r'''
eval "orig_$(declare -f net)"
net() {
  if [[ ! -e armed && ! -e lost && $SSH_HOST == exit-* && $* == */dev/tcp/* ]]; then
    touch lost
    echo "${SSH_HOST#exit-}" >> probes
    return 1
  fi
  orig_net "$@"
}
'''


class EntryBeatSlowExitTests(unittest.TestCase):
    def run_slow(self, login, files):
        runner = refs.EntryBeatReferenceExitTests()
        with mock.patch.object(refs, 'NET', refs.NET + f'LOGIN={login}\n' + SLOW):
            return runner.run_entry_stage(exit_tags='se', files=files)

    def test_slow_exit_sees_stage_closing_entry_and_stage_rolls_back(self):
        for login in (8, 13):
            with self.subTest(login=login):
                result, beats, probes, detail = self.run_slow(login, ('outside-open', 'stage-closes'))
                self.assertTrue(probes, 'экзит не спрошен до стадии\n' + detail)
                self.assertNotIn('entry-closed', beats, 'пульс через lo при закрытом стадией входе\n' + detail)
                self.assertIn('откатан', result.stdout, detail)
                self.assertIn('RESULT=1', result.stdout, detail)

    def test_slow_exit_beats_while_entry_stays_open(self):
        for login in (8, 13):
            with self.subTest(login=login):
                result, beats, probes, detail = self.run_slow(login, ('outside-open',))
                self.assertIn('entry', beats, detail)
                self.assertNotIn('entry-closed', beats, detail)
                self.assertNotIn('не проверяется', result.stdout, detail)
                self.assertNotIn('откатан', result.stdout, detail)
                self.assertIn('RESULT=0', result.stdout, detail)

    def test_lost_baseline_probe_keeps_exit_as_reference(self):
        runner = refs.EntryBeatReferenceExitTests()
        with mock.patch.object(refs, 'NET', refs.NET + LOST_BASELINE):
            result, beats, probes, detail = runner.run_entry_stage(
                exit_tags='se', files=('outside-open', 'stage-closes'))
        self.assertNotIn('не проверяется', result.stdout, 'одна потерянная проба оставила стадию без опорных\n' + detail)
        self.assertNotIn('entry-closed', beats, 'пульс через lo при закрытом стадией входе\n' + detail)
        self.assertIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=1', result.stdout, detail)


if __name__ == '__main__':
    unittest.main()
