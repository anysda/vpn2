"""Предел ssh гасит дерево и сессию, не обходя таблицу процессов хоста.

`ps -e` и `pkill` читают /proc каждого процесса хоста: под нагрузкой (десятки
тысяч процессов) один такой обход стоит секунды, и возврат _limit_run
опаздывает от предела на несколько обходов. Потомков видно от корня по
/proc/<pid>/task/*/children - стоимость равна размеру дерева. Подставные ps и
pkill в PATH пишут каждый вызов и зовут настоящие. По пределу, по раннему
концу команды и в самом _limit_kill_tree обхода хоста нет, а дерево (дети,
внук, ребёнок в своей сессии) и сирота в сессии убиты. Без children
(_limit_children_ok ложно, ядро без CONFIG_PROC_CHILDREN) работает прежний
снимок ps, и всё так же убито. Обе копии (infra/lib/ssh.sh и 99-verify).
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_ssh_limit_session import preamble

SPY = '''#!/bin/bash
echo "$(basename "$0") $*" >> "$SPY_CALLS"
exec /usr/bin/$(basename "$0") "$@"
'''

# Дерево: два ребёнка, внук, ребёнок в своей сессии; сирота в сессии команды
# (родитель вышел). Каждый пишет «метка pid» в $PIDS; корень ждёт всех.
TREE = r'''
echo "root $$" >> "$PIDS"
sleep 60 </dev/null >/dev/null 2>&1 & echo "kid $!" >> "$PIDS"
bash -c 'sleep 60 & echo "grandkid $!" >> "$PIDS"; wait' </dev/null >/dev/null 2>&1 & echo "kid $!" >> "$PIDS"
setsid sleep 60 </dev/null >/dev/null 2>&1 & echo "setsid $!" >> "$PIDS"
( sleep 60 </dev/null >/dev/null 2>&1 & echo "orphan $!" >> "$PIDS" )
until (( $(wc -l < "$PIDS") >= 6 )); do sleep 0.01; done
[[ ${1-} == early ]] && exit 0
wait
'''

SCENARIO = r'''
alive() { local s; s=$(awk '{ print $3 }' "/proc/$1/stat" 2>/dev/null) && [[ -n $s && $s != Z ]]; }
# $1 - метка, что не наша: её процесс не проверяется, а гасится в конце.
left() {
  local tag k i out=''
  while read -r tag k; do
    if [[ $tag == "$1" ]]; then kill "$k" 2>/dev/null || :; continue; fi
    for ((i = 0; i < 200; i++)); do alive "$k" || break; sleep 0.01; done
    ! alive "$k" || out+=" $tag:$k"
  done < "$PIDS"
  echo "${out:-none}"
}
case $CASE in
  # Сирота ушла от корня: _limit_kill_tree гасит только потомков.
  tree)
    bash "$TREE" </dev/null >/dev/null 2>&1 & root=$!
    until [[ -f $PIDS ]] && (( $(wc -l < "$PIDS") >= 6 )); do sleep 0.01; done
    : > "$SPY_CALLS"
    _limit_kill_tree "$root"
    wait "$root" 2>/dev/null || :
    echo "left $(left orphan)" ;;
  limit)
    rc=0; _limit_run 2 bash "$TREE" </dev/null >/dev/null 2>&1 || rc=$?
    echo "rc $rc left $(left -)" ;;
  # Ребёнок в своей сессии переживает ранний конец и с pkill -s: не наш.
  early)
    rc=0; _limit_run 30 bash "$TREE" early </dev/null >/dev/null 2>&1 || rc=$?
    echo "rc $rc left $(left setsid)" ;;
esac
'''

FALLBACK = '_limit_children_ok() { return 1; }\n'


def host_walks(calls):
    """Вызовы, что обходят весь /proc: pkill любой, ps без -p."""
    return [line for line in calls if line.startswith('pkill ')
            or (line.startswith('ps ') and '-p' not in line.split()[1:])]


class TestLimitProcWalk(unittest.TestCase):
    def run_case(self, side, case, fallback=False):
        with tempfile.TemporaryDirectory(prefix='limit-proc-walk-') as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            for name in ('ps', 'pkill'):
                (root / 'bin' / name).write_text(SPY)
                (root / 'bin' / name).chmod(0o755)
            (root / 'tree.sh').write_text(TREE)
            calls, pids = root / 'calls', root / 'pids'
            env = {'PATH': f'{root}/bin:/usr/bin:/bin', 'SPY_CALLS': str(calls), 'PIDS': str(pids),
                   'TREE': str(root / 'tree.sh'), 'CASE': case}
            script = preamble(side) + (FALLBACK if fallback else '') + SCENARIO
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], env=env,
                                    text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lines = calls.read_text().splitlines() if calls.exists() else []
            return result.stdout.strip(), lines

    def test_no_host_walk_tree_dead(self):
        for side in ('lib', 'node'):
            for case, rc in (('tree', None), ('limit', '137'), ('early', '0')):
                with self.subTest(side=side, case=case):
                    out, calls = self.run_case(side, case)
                    print(f'{side} {case}: {out}; обходы хоста: {host_walks(calls) or "нет"}')
                    self.assertTrue(out.endswith('left none'), f'{side} {case}: выжили: {out}')
                    if rc:
                        self.assertTrue(out.startswith(f'rc {rc} '), f'{side} {case}: код {out}')
                    self.assertEqual(host_walks(calls), [], f'{side} {case}: обход всего /proc')

    def test_without_children_falls_back_to_snapshot(self):
        for side in ('lib', 'node'):
            for case in ('tree', 'limit', 'early'):
                with self.subTest(side=side, case=case):
                    out, calls = self.run_case(side, case, fallback=True)
                    print(f'{side} {case} без children: {out}; вызовы: {calls}')
                    self.assertTrue(out.endswith('left none'), f'{side} {case}: выжили: {out}')
                    self.assertTrue(host_walks(calls), f'{side} {case}: без children нет прежнего снимка ps')


if __name__ == '__main__':
    unittest.main()
