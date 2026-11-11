"""Пульс сторожа фаервола кончается вместе со стадией, проба экзитов не виснет.

Замок деплоя - живой coproc главной оболочки. Стадия экзитов ведёт хосты по
очереди в ней же, и проба «видит ли экзит вход на entry» шла подстановкой
`$(...)` с голым `wait`: bash 5.3 там без конца пишет «wait: pid N is not a
child of this shell».

VPN2_TEST_ROOT позволяет прогнать те же проверки против другой ревизии.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from play_stage import PLAY_STUBS, play_tree, run_play_source
from test_fw_guard_entry_beat import functions


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))

STUBS = '''
log() { :; }
ok() { :; }
warn() { echo "WARN $*"; }
die() { echo "DIE $*"; exit 1; }
push() { :; }
load_env() { HOST_TAG=$1; }
sleep() { command sleep 0.1; }
ssh_exec() { echo "exec $*" >> calls; [[ $* != *"/tmp/anysda/$STAGE.sh"* ]] || { command sleep 0.5; return "$STAGE_RC"; }; }
ssh_fresh() { [[ $* != *'fw-guard.sh beat'* ]] || echo beat >> beats; }
beats() { wc -l < beats 2>/dev/null || echo 0; }
# Пульс замер: за время в несколько шагов пульса новых не пришло.
settled() {
  command sleep 0.5
  local before; before=$(beats)
  command sleep 1
  echo "BEATS=$before AFTER=$(beats)"
}
'''


def deploy(body, timeout=60):
    source = (ROOT / 'infra/deploy.sh').read_text()
    settings = ''.join(line + '\n' for line in source.splitlines()
                       if line.startswith(('FW_GUARD_STAGES=', 'FW_GUARD_WINDOW=')))
    script = (settings + functions(ROOT / 'infra/lib/ssh.sh') + functions(ROOT / 'infra/deploy.sh')
              + run_play_source() + STUBS + PLAY_STUBS + 'DEPLOY_ROOT=$PWD\nSKIPPED_EXIT_TAGS=\n' + body)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for path in ('envs', 'lib'):
            (root / path).mkdir()
        play_tree(root, ['29-openvpn'], STUBS)
        (root / 'envs/all.env').write_text('COMMON=1\n')
        for host in ('ru', 'se'):
            (root / f'envs/{host}.env').write_text(f'HOST_TAG={host}\n')
        (root / 'envs/exits.env').write_text("EXIT_TAGS='se'\n")
        try:
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=directory, text=True,
                                    capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise AssertionError(f'не завершилось за {timeout} с: {str(error.stderr)[:300]}') from None
        calls = (root / 'calls').read_text() if (root / 'calls').exists() else ''
        return result, calls


def settled(test, result):
    out = result.stdout
    line = next((line for line in out.splitlines() if line.startswith('BEATS=')), None)
    test.assertIsNotNone(line, out + result.stderr)
    before, after = (int(part.split('=')[1]) for part in line.split())
    test.assertGreater(before, 0, 'пульса не было вовсе: ' + out + result.stderr)
    test.assertEqual(before, after, 'пульс идёт после стадии: ' + out + result.stderr)


class BeatStopsWithStageTests(unittest.TestCase):
    def stage(self, rc):
        return deploy(f'''
STAGE=29-openvpn STAGE_RC={rc}
export STAGE_RC
HOST_TAG=se
if run_stage_on_host 29-openvpn se; then echo RESULT=0; else echo RESULT=1; fi
settled
''')[0]

    def test_beat_stops_after_stage_success(self):
        result = self.stage(0)
        self.assertIn('RESULT=0', result.stdout, result.stderr)
        settled(self, result)

    def test_beat_stops_after_stage_failure(self):
        result = self.stage(1)
        self.assertIn('RESULT=1', result.stdout, result.stderr)
        settled(self, result)

    def shell_exit(self, code):
        # Оболочка стадии вышла мимо останова пульса (die, сигнал), деплой жив.
        return deploy(f'''
HOST_TAG=se
( owner=$BASHPID; fw_guard_beat "$owner" & command sleep 0.5; exit {code} ) || echo "STAGE_EXIT=$?"
settled
''')[0]

    def test_beat_stops_when_stage_shell_exits_clean(self):
        settled(self, self.shell_exit(0))

    def test_beat_stops_when_stage_shell_dies(self):
        result = self.shell_exit(1)
        self.assertIn('STAGE_EXIT=1', result.stdout, result.stderr)
        settled(self, result)


class ProbeUnderDeployLockTests(unittest.TestCase):
    def test_exit_probe_returns_with_live_coproc(self):
        result, _ = deploy('''
fw_guard_exit_sees_entry() { command sleep 0.1; [[ $1 == se ]]; }
coproc LOCK { exec sleep 20; }
codes=$(fw_guard_exits_see_entry 'se nl')
echo "CODES=$(sort <<< "$codes" | tr '\\n' ';')"
rc=0; fw_guard_entry_probe 'se nl' || rc=$?
echo "PROBE=$rc"
kill "$LOCK_PID"
''', timeout=15)
        self.assertNotIn('not a child of this shell', result.stderr)
        self.assertIn('CODES=nl 1;se 0;', result.stdout, result.stderr)
        self.assertIn('PROBE=0', result.stdout)


class ExitStageOnEntryTests(unittest.TestCase):
    BODY = '''
warn_insecure_exits() { :; }
run_stage_on_host() { echo "HOST $1 $2"; }
'''

    def test_all_skips_entry(self):
        result, _ = deploy(self.BODY + 'run_stage 10-foreign all\necho DONE\n')
        hosts = [line for line in result.stdout.splitlines() if line.startswith('HOST ')]
        self.assertEqual(hosts, ['HOST 10-foreign se', 'HOST 99-verify se'], result.stdout + result.stderr)
        self.assertIn('DONE', result.stdout)

    def test_entry_alone_is_refused_with_reason(self):
        result, _ = deploy(self.BODY + 'run_stage 10-foreign ru\necho DONE\n')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('DIE 10-foreign - стадия экзитов, на entry её нет', result.stdout)
        self.assertNotIn('HOST ', result.stdout)


if __name__ == '__main__':
    unittest.main()
