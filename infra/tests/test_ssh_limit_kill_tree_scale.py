"""Проход _limit_kill_tree не растёт с числом процессов на хосте.

Убийца по пределу строит дерево потомков по снимку `ps -e`. Разбор снимка
циклом bash `read` стоит секунды при десятках тысяч процессов, и возврат
_limit_run опаздывает от предела на столько же. Подставной ps добавляет к
настоящему снимку 20 тыс. чужих строк и цепочку несуществующих потомков (pid
выше pid_max: kill по ним ничего не задевает). Порог - не постоянная: замер
того же числа проходов `ps | awk` по тому же снимку на этом раннере, x3.
Обе копии (infra/lib/ssh.sh и 99-verify) проверяются одинаково.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_ssh_limit_session import preamble

ROWS = 20000
CHAIN = 3
TIMES = 3
FACTOR = 3

FAKE_PS = '''#!/bin/bash
/usr/bin/ps "$@"
cat "$FAKE_PS_EXTRA"
'''

# $1 - число повторов. Корень - настоящий bash с двумя детьми; к нему в снимке
# цепочка CHAIN мнимых потомков, записанных в обратном порядке: ребёнок
# раньше родителя. На повтор строка «base <с> tree <с> left <живые>»: base -
# PASSES проходов ps | awk по тому же снимку, tree - _limit_kill_tree, left -
# выжившие из настоящего дерева.
SCENARIO = r'''
alive() { [[ -d /proc/$1 ]] && [[ $(awk '{ print $3 }' "/proc/$1/stat" 2>/dev/null) != Z ]]; }
since() { awk -v s="$1" -v e="$EPOCHREALTIME" 'BEGIN { printf "%.4f", e - s }'; }
for ((r = 0; r < $1; r++)); do
  bash -c 'sleep 60 & sleep 60 & wait' </dev/null >/dev/null 2>&1 & root=$!
  until mapfile -t kids < <(/usr/bin/ps -o pid= --ppid "$root"); (( ${#kids[@]} == 2 )); do sleep 0.01; done
  { for ((i = 0; i < CHAIN; i++)); do echo "$((MAXPID + CHAIN - i)) $((MAXPID + CHAIN - i - 1))"; done
    echo "$MAXPID $root"; } > "$FAKE_PS_EXTRA.chain"
  cat "$FAKE_PS_EXTRA.rows" "$FAKE_PS_EXTRA.chain" > "$FAKE_PS_EXTRA"
  s=$EPOCHREALTIME
  for ((i = 0; i < PASSES; i++)); do ps -e -o pid= -o ppid= | awk '{ a[$2] = a[$2] " " $1 }'; done
  base=$(since "$s")
  s=$EPOCHREALTIME
  _limit_kill_tree "$root"
  tree=$(since "$s")
  wait "$root" 2>/dev/null || :
  left=''
  for k in "$root" "${kids[@]}"; do
    for ((i = 0; i < 100; i++)); do alive "$k" || break; sleep 0.01; done
    ! alive "$k" || left+=" $k"
  done
  echo "base $base tree $tree left ${left:-none}"
done
'''


class TestKillTreeScale(unittest.TestCase):
    def run_side(self, side, directory):
        root = Path(directory)
        (root / 'bin').mkdir()
        ps = root / 'bin' / 'ps'
        ps.write_text(FAKE_PS)
        ps.chmod(0o755)
        maxpid = int(Path('/proc/sys/kernel/pid_max').read_text()) + 1000
        extra = root / 'extra'
        # Чужие процессы: ни один не потомок корня, pid выше pid_max.
        rows = ''.join(f'{maxpid + 10000 + i} {maxpid + 10000 + i - 1}\n' for i in range(ROWS))
        Path(f'{extra}.rows').write_text(rows)
        # Цепочка в обратном порядке: построчный разбор берёт по звену за
        # проход, сборка по снимку - всё за первый, второй находит пусто.
        # База - те же два прохода ps | awk.
        env = {'PATH': f'{root}/bin:/usr/bin:/bin', 'FAKE_PS_EXTRA': str(extra), 'MAXPID': str(maxpid),
               'CHAIN': str(CHAIN), 'PASSES': '2'}
        script = preamble(side) + SCENARIO
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script, '_', str(TIMES)], env=env,
                                text=True, capture_output=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr)
        runs = [line.split(maxsplit=5) for line in result.stdout.splitlines()]
        self.assertEqual(len(runs), TIMES, result.stdout)
        left = ' '.join(run[5] for run in runs if run[5] != 'none') or 'none'
        return min(float(run[1]) for run in runs), min(float(run[3]) for run in runs), left

    def test_one_pass_does_not_grow_with_process_count(self):
        for side in ('lib', 'node'):
            with self.subTest(side=side), tempfile.TemporaryDirectory(prefix='kill-tree-scale-') as directory:
                base, tree, left = self.run_side(side, directory)
                bound = FACTOR * base
                print(f'_limit_kill_tree {side}: {ROWS} строк, цепочка {CHAIN}: {tree:.3f}s, '
                      f'база ps|awk x2 {base:.3f}s, порог {bound:.3f}s')
                self.assertEqual(left, 'none', f'{side}: дерево не убито: {left}')
                self.assertLess(tree, bound, f'{side}: проход по снимку {ROWS} строк {tree:.3f}s, порог {bound:.3f}s')


if __name__ == '__main__':
    unittest.main()
