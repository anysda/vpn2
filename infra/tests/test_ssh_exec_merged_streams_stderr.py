"""ssh_exec в один файл с stderr: строки stdout не затираются копией stderr.

Как в test_ssh_exec_merged_streams.py: `ssh_exec ... >"$log" 2>&1`, настоящий
sshd на 127.0.0.1. Здесь узел пишет в stderr раньше и больше, чем в stdout:
после метки _ssh_errfilter копирует stderr в журнал, пока _ssh_output пишет
туда же stdout. uutils cat копирует в файл через splice и затирает строки
второго писателя: на стенде от stdout оставалось 0 строк из 2000. Гонку даёт
uutils cat, поэтому без него тест пропускается с причиной.
"""
import re
import subprocess
import unittest

import test_ssh_exec_merged_streams as merged

ERR_LINES = 20000
OUT_LINES = 5000
RUNS = 5
# stderr начинает раньше и идёт дольше; stdout идёт параллельно с ним.
LOOP = (f'{{ for i in $(seq {ERR_LINES}); do echo e$i; done >&2 & }}; '
        f'for i in $(seq {OUT_LINES}); do echo o$i; done; wait; exit 7')


def uutils_cat():
    out = subprocess.run(['cat', '--version'], capture_output=True, text=True).stdout
    return 'uutils' in out


class SshExecMergedStderrTests(merged.SshExecMergedStreamsTests):
    @classmethod
    def setUpClass(cls):
        if not uutils_cat():
            raise unittest.SkipTest('cat не uutils: затирание через splice воспроизводится только с uutils cat')
        super().setUpClass()

    def test_merged_output_keeps_every_line_and_code(self):
        self.skipTest('прогоны с поровну строк идут в test_ssh_exec_merged_streams.py')

    def test_stderr_heavy_output_keeps_stdout_lines(self):
        want = {f'o{i}' for i in range(1, OUT_LINES + 1)} | {f'e{i}' for i in range(1, ERR_LINES + 1)}
        size = sum(len(s) + 1 for s in want)
        bad = []
        for tag in ('us', 'ru'):
            for attempt in range(RUNS):
                result, text = self.run_way(tag)
                self.assertEqual((result.returncode, result.stdout), (0, 'rc=7\n'),
                                 result.stderr + (self.root / 'sshd.log').read_text()[-2000:])
                lines = text.splitlines()
                missing = want - set(lines)
                if missing or len(text.encode()) != size:
                    bad.append(f'{tag} #{attempt}: байт {len(text.encode())} из {size}, '
                               f'нет o {sum(m[0] == "o" for m in missing)}, e {sum(m[0] == "e" for m in missing)}, '
                               f'обрывки {[s for s in lines if not re.fullmatch(r"[oe][0-9]+", s)][:3]}')
        self.assertEqual(bad, [], f'журнал стадии потерял строки в {len(bad)} прогонах из {2 * RUNS}')

    def run_way(self, tag):
        merged.WAYS['stderr'] = f"rc=0; ssh_exec '{LOOP}' >\"$log\" 2>&1 || rc=$?"
        try:
            return self.run_merged('stderr', tag)
        finally:
            del merged.WAYS['stderr']


if __name__ == '__main__':
    unittest.main()
