"""Пульс сторожа на entry различает «стадия закрыла вход» и «экзит не дошёл».

Время стенда ускорено в 10 раз: sleep N спит N/10 с, сторож считает окно в тех
же секундах деплоя по реальным часам. Сторож откатывает фаервол, если с
последнего пульса (или с arm) прошло больше окна, поэтому временные края видны:
лишнее ожидание в disarm приводит к откату.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_fw_guard_entry_beat import ROOT, STUBS, functions


WINDOW = 30

NET = r'''
sleep() { command sleep "$(awk -v s="$1" 'BEGIN { print s / 10 }')"; }
now_ms() { echo $(( $(date +%s%N) / 1000000 )); }
load_env() {
  case $1 in
    ru) HOST_TAG=ru; SSH_HOST=entry-node; SSH_PASS=entry ;;
    se) HOST_TAG=se; SSH_HOST=exit-se; SSH_PASS=exit ;;
    'fi') HOST_TAG=fi; SSH_HOST=exit-fi; SSH_PASS=exit ;;
    *) return 1 ;;
  esac
}
net() {
  local node cmd="$*" last age n
  case $SSH_HOST in
    entry-node) node=entry ;;
    exit-se) node=se ;;
    exit-fi) node=fi ;;
    *) return 255 ;;
  esac
  echo "$node $cmd" >> calls
  if [[ $node == entry ]]; then
    case $cmd in
      *"/tmp/anysda/$STAGE.sh"*)
        now_ms > armed
        if [[ -e stage-closes ]]; then
          rm -f outside-open
          # Проба, ушедшая до закрытия, может донести пульс чуть позже.
          command sleep 0.3
          touch closed
        fi
        command sleep 1.5
        touch stage-done ;;
      *'fw-guard.sh beat'*)
        now_ms > beat
        if [[ -e closed || ( ! -e outside-open && ! -e stage-closes ) ]]; then echo entry-closed >> beats; else echo entry >> beats; fi ;;
      *'fw-guard.sh disarm'*)
        last=$(cat armed)
        [[ ! -e beat ]] || last=$(cat beat)
        age=$(( ($(now_ms) - last) / 100 ))
        if (( age > FW_GUARD_WINDOW )); then
          echo "vpn2-fw-guard: пульса не было ${age} с, фаервол откатан"
          return 3
        fi ;;
    esac
    return 0
  fi
  [[ ! -e down-$node ]] || return 255
  if [[ $cmd == */dev/tcp/* ]]; then
    echo "$node" >> probes
    if [[ -e disarming && -s lose ]]; then
      read -r n < lose
      if (( n > 0 )); then echo $(( n - 1 )) > lose; return 1; fi
    fi
    [[ ! -e nopath-$node ]] || return 1
    # Экзит, которому дали loopback, видит собственный sshd.
    [[ $ENTRY_HOST != 127.* ]] || return 0
    [[ -e outside-open ]] || return 1
  fi
  return 0
}
ssh_exec() { net "$@"; }
ssh_fresh() { net "$@"; }
eval "orig_$(declare -f fw_guard_disarm)"
fw_guard_disarm() { touch disarming; orig_fw_guard_disarm "$@"; }
'''


class EntryBeatReferenceExitTests(unittest.TestCase):
    def run_entry_stage(self, entry_host='198.51.100.10', exit_tags='se fi', files=(), lose=0,
                        stage='28-wireguard'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ('scripts', 'envs', 'lib'):
                (root / path).mkdir()
            (root / 'scripts' / f'{stage}.sh').touch()
            (root / 'envs/all.env').write_text(f'ENTRY_HOST={entry_host}\n')
            for tag in ('ru', 'se', 'fi'):
                (root / f'envs/{tag}.env').write_text(f'HOST_TAG={tag}\n')
            (root / 'envs/exits.env').write_text(f"EXIT_TAGS='{exit_tags}'\n")
            for name in files:
                (root / name).touch()
            if lose:
                (root / 'lose').write_text(f'{lose}\n')
            script = (functions(ROOT / 'infra/lib/ssh.sh') + functions(ROOT / 'infra/deploy.sh')
                      + STUBS + NET + f'''
FW_GUARD_STAGES='{stage}'
FW_GUARD_WINDOW={WINDOW}
ENTRY_HOST={entry_host}
DEPLOY_ROOT=$PWD
STAGE={stage}
SKIPPED_EXIT_TAGS=''
HOST_TAG=ru
if run_stage_on_host {stage} ru; then echo RESULT=0; else echo RESULT=1; fi
''')
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=directory,
                                    text=True, capture_output=True, timeout=90)
            beats = (root / 'beats').read_text().split() if (root / 'beats').exists() else []
            probes = (root / 'probes').read_text().split() if (root / 'probes').exists() else []
            calls = (root / 'calls').read_text() if (root / 'calls').exists() else ''
            return result, beats, probes, result.stdout + result.stderr + calls

    def test_exit_without_path_does_not_hide_open_entry_seen_by_other_exit(self):
        result, beats, probes, detail = self.run_entry_stage(files=('outside-open', 'nopath-se'))
        self.assertIn('fi', probes, 'второй экзит не спрошен\n' + detail)
        self.assertGreater(beats.count('entry'), 1, detail)
        self.assertNotIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=0', result.stdout, detail)

    def test_one_lost_probe_at_disarm_does_not_roll_back_healthy_stage(self):
        result, beats, probes, detail = self.run_entry_stage(files=('outside-open',), lose=1)
        self.assertIn('entry', beats, detail)
        self.assertNotIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=0', result.stdout, detail)

    def test_entry_behind_nat_beats_over_lo_with_one_warn(self):
        result, beats, probes, detail = self.run_entry_stage(entry_host='10.20.30.40')
        self.assertTrue(probes, 'экзиты не спрошены до стадии\n' + detail)
        self.assertGreater(beats.count('entry-closed'), 1, detail)
        self.assertEqual(result.stdout.count('вход на entry снаружи не проверяется'), 1, detail)
        self.assertIn('entry за NAT', result.stdout, detail)
        self.assertNotIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=0', result.stdout, detail)

    def test_loopback_entry_host_is_not_checked_and_warns(self):
        result, beats, probes, detail = self.run_entry_stage(entry_host='127.0.0.1',
                                                             files=('outside-open',))
        self.assertEqual(probes, [], 'экзит проверял loopback, то есть свой sshd\n' + detail)
        self.assertEqual(result.stdout.count('вход на entry снаружи не проверяется'), 1, detail)
        self.assertIn('локальный', result.stdout, detail)
        self.assertIn('entry', beats, detail)
        self.assertIn('RESULT=0', result.stdout, detail)

    def test_stage_that_closes_entry_seen_before_by_exits_is_rolled_back(self):
        result, beats, probes, detail = self.run_entry_stage(files=('outside-open', 'stage-closes'))
        self.assertNotIn('entry-closed', beats, 'пульс при закрытом стадией входе\n' + detail)
        self.assertIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=1', result.stdout, detail)

    def test_stage_that_closes_entry_rolls_back_although_one_exit_has_no_path(self):
        result, beats, probes, detail = self.run_entry_stage(files=('outside-open', 'stage-closes',
                                                                    'nopath-se'))
        self.assertNotIn('entry-closed', beats, 'пульс при закрытом стадией входе\n' + detail)
        self.assertIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=1', result.stdout, detail)


if __name__ == '__main__':
    unittest.main()
