"""Проверка «жив» в тестах предела не считает живым процесс, пожатый на чтении.

Убитый потомок, чей родитель ушёл, - зомби у init, и на загруженном раннере
init жнёт его не сразу. alive() видит /proc/<pid>, а к чтению stat запись уже
пропала: пустое состояние не равно Z, и пожатый процесс выходил живым
(«left kid:<pid>» после предела). Заглушка awk открывает это окно без
надежды на тайминг: убивает процесс и ждёт, пока его пожнут, потом читает.
Копия alive() в kill_tree_scale.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_ssh_limit_kill_tree_scale

# Только чтение stat цели: убить, дождаться, пока родитель пожнёт, читать.
AWK = '''#!/bin/bash
if [[ ${!#} == /proc/$TARGET/stat ]]; then
  kill -KILL "$TARGET"
  while [[ -e /proc/$TARGET ]]; do sleep 0.01; done
fi
exec /usr/bin/awk "$@"
'''

# Родитель жнёт сам: wait вернётся, как только цель убита.
SCENARIO = r'''
bash -c 'sleep 60 & echo $! > "$PIDFILE"; wait' </dev/null >/dev/null 2>&1 & keeper=$!
until [[ -s $PIDFILE ]]; do sleep 0.01; done
export TARGET=$(< "$PIDFILE")
if alive "$TARGET"; then echo alive; else echo gone; fi
wait "$keeper" 2>/dev/null || :
'''


def alive_line(module):
    return next(line for line in module.SCENARIO.splitlines() if line.startswith('alive()'))


class TestAliveReaped(unittest.TestCase):
    def test_reaped_between_dir_and_stat_is_gone(self):
        for module in (test_ssh_limit_kill_tree_scale,):
            with self.subTest(module=module.__name__), \
                    tempfile.TemporaryDirectory(prefix='limit-alive-') as directory:
                root = Path(directory)
                (root / 'bin').mkdir()
                (root / 'bin' / 'awk').write_text(AWK)
                (root / 'bin' / 'awk').chmod(0o755)
                env = {'PATH': f'{root}/bin:/usr/bin:/bin', 'PIDFILE': str(root / 'pid')}
                script = alive_line(module) + '\n' + SCENARIO
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], env=env,
                                        text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(f'{module.__name__}: {result.stdout.strip()}')
                self.assertEqual(result.stdout.strip(), 'gone',
                                 f'{module.__name__}: пожатый процесс считан живым')


if __name__ == '__main__':
    unittest.main()
