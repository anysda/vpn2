"""Пульс сторожа фаервола на entry должен доказывать вход снаружи.

deploy.sh всегда идёт с entry, и ssh с entry к самой entry (хоть по внешнему
адресу) ходит через lo. Заглушки ssh_exec/ssh_fresh моделируют это: команда на
entry проходит всегда, команда на экзите к адресу entry проходит, только если
внешний вход открыт. Сторож откатывает фаервол, если за стадию не было пульса.

VPN2_TEST_ROOT позволяет прогнать те же проверки против другой ревизии.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
ENTRY = '198.51.100.10'
EXIT = '203.0.113.20'


def functions(path):
    """Все функции верхнего уровня файла, в порядке объявления."""
    out, body = [], None
    for line in path.read_text().splitlines():
        if body is None:
            if re.match(r'^[A-Za-z_][A-Za-z0-9_]*\(\) *\{', line):
                if line.rstrip().endswith('}'):
                    out.append(line + '\n')
                else:
                    body = [line]
        else:
            body.append(line)
            if line == '}':
                out.append('\n'.join(body) + '\n')
                body = None
    return ''.join(out)


STUBS = f'''
ENTRY_HOST={ENTRY}
SSH_USER=root
load_env() {{
  case $1 in
    ru) HOST_TAG=ru; SSH_HOST={ENTRY}; SSH_PASS=entry ;;
    se) HOST_TAG=se; SSH_HOST={EXIT}; SSH_PASS=exit ;;
    *) return 1 ;;
  esac
}}
log() {{ :; }}
ok() {{ :; }}
warn() {{ echo "WARN $*"; }}
die() {{ echo "DIE $*"; exit 1; }}
push() {{ :; }}
sleep() {{ command sleep 0.05; }}

# Сеть стенда. Каждая команда узлу пишется в calls как «узел команда».
node_cmd() {{
  local node cmd="$*"
  case $SSH_HOST in
    {ENTRY}) node=entry ;;
    {EXIT}) node=exit ;;
    *) echo "unknown host $SSH_HOST" >> calls; return 255 ;;
  esac
  echo "$node $cmd" >> calls
  if [[ $cmd == *"/tmp/anysda/$STAGE.sh"* ]]; then
    if [[ -e stage-closes ]]; then
      # Стадия закрыла вход снаружи. Пульс, ушедший до закрытия, долетает, а arm
      # сторожа считает окно заново: прежние пульсы не в счёт.
      rm -f external-open
      command sleep 0.3
      rm -f beats
    fi
    # Стадия идёт, пока сторож не получил хотя бы одну попытку пульса.
    local i start
    start=$(wc -l < calls)
    for i in $(seq 60); do
      (( $(wc -l < calls) > start )) && break
      command sleep 0.05
    done
    command sleep 0.5
    return 0
  fi
  if [[ $node == exit && $cmd == *{ENTRY}* ]]; then
    # С экзита к entry: проходит только при открытом внешнем входе.
    [[ -e external-open ]] || return 1
    [[ $cmd != *'fw-guard.sh beat'* ]] || echo entry >> beats
    return 0
  fi
  case $cmd in
    *'fw-guard.sh beat'*) echo "$node" >> beats ;;
    *'fw-guard.sh disarm'*)
      if grep -qx "$node" beats 2>/dev/null; then return 0; fi
      echo 'vpn2-fw-guard: пульса не было, фаервол откатан к состоянию до стадии'
      return 3 ;;
  esac
  return 0
}}
ssh_exec() {{ node_cmd "$@"; }}
ssh_fresh() {{ node_cmd "$@"; }}
'''


class EntryGuardBeatTests(unittest.TestCase):
    def run_stage(self, host, external_open, stage='28-wireguard'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ('scripts', 'envs', 'lib'):
                (root / path).mkdir()
            (root / 'scripts' / f'{stage}.sh').touch()
            (root / 'envs/all.env').write_text(f'ENTRY_HOST={ENTRY}\n')
            (root / 'envs/ru.env').write_text('HOST_TAG=ru\n')
            (root / 'envs/se.env').write_text('HOST_TAG=se\n')
            (root / 'envs/exits.env').write_text("EXIT_TAGS='se'\n")
            # Вход снаружи открыт до стадии; external_open=False - его закрывает стадия.
            (root / 'external-open').touch()
            if not external_open:
                (root / 'stage-closes').touch()
            source = (ROOT / 'infra/deploy.sh').read_text()
            settings = ''.join(line + '\n' for line in source.splitlines()
                               if line.startswith(('FW_GUARD_STAGES=', 'FW_GUARD_WINDOW=')))
            script = (settings + functions(ROOT / 'infra/lib/ssh.sh')
                      + functions(ROOT / 'infra/deploy.sh') + STUBS + f'''
DEPLOY_ROOT=$PWD
STAGE={stage}
SKIPPED_EXIT_TAGS=''
HOST_TAG=ru
if run_stage_on_host {stage} {host}; then echo RESULT=0; else echo RESULT=1; fi
''')
            env = dict(os.environ, VPN2_FW_GUARD_WINDOW='1')
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=directory,
                                    env=env, text=True, capture_output=True, timeout=90)
            beats = (root / 'beats').read_text().split() if (root / 'beats').exists() else []
            calls = (root / 'calls').read_text() if (root / 'calls').exists() else ''
            return result, beats, calls

    def test_entry_closed_from_outside_gets_no_beat_and_stage_fails(self):
        result, beats, calls = self.run_stage('ru', external_open=False)
        detail = result.stdout + result.stderr + calls
        self.assertNotIn('entry', beats, 'сторож entry получил пульс через lo при закрытом внешнем входе\n' + detail)
        self.assertIn('RESULT=1', result.stdout, detail)

    def test_entry_open_from_outside_beats_and_keeps_stage_firewall(self):
        result, beats, calls = self.run_stage('ru', external_open=True)
        detail = result.stdout + result.stderr + calls
        self.assertIn('entry', beats, detail)
        self.assertNotIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=0', result.stdout, detail)

    def test_exit_stage_beats_by_direct_ssh_to_exit(self):
        result, beats, calls = self.run_stage('se', external_open=True)
        detail = result.stdout + result.stderr + calls
        self.assertIn('exit bash --norc /run/vpn2-fw-guard/fw-guard.sh beat', calls, detail)
        self.assertIn('exit', beats, detail)
        self.assertNotIn('откатан', result.stdout, detail)
        self.assertIn('RESULT=0', result.stdout, detail)


if __name__ == '__main__':
    unittest.main()
