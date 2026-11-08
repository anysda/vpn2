"""_ssh_output и _ssh_errfilter, пишущие в один файл, не теряют строк друг друга.

Журнал стадии - `>"$log" 2>&1`: после метки stdout пишет _ssh_copy_lines из
_ssh_output, stderr - _ssh_copy_lines из _ssh_errfilter, оба через одно
открытие файла. _ssh_copy_lines (perl построчно) отдаёт каждую строку одним
write() и файл не режет. Писатель, что при выходе делает ftruncate по своему
сдвигу (так делал uutils dd без conv=notrunc), отрезает хвост, который второй
писатель в тот же миг дописал, а следующая запись оставляет дыру из NUL.
Окно - пара системных вызовов, поэтому прогонов много: в каждом один поток
короткий, другой длинный и идёт через sed (записи по 4 КиБ), так что
короткий писатель выходит посреди записи длинного. Короткой стороной по
очереди бывают обе функции. Строки по 8 байт: размер журнала, потерянные
строки и NUL сверяются точно.

Вход функциям подаётся на stdin с меткой, как его отдаёт ssh; сети нет.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT

RUNS = 1000
SHORT, LONG = 500, 20000
# $1 - ssh.sh, $2 - каталог, $3 - число прогонов.
RACE = r'''source "$1"
set +e
w=$2
gen() { seq 100001 $((100000 + $2)) | sed "s/^/$1/"; }
gen e $SHORT > "$w/e_short"; gen e $LONG > "$w/e_long"
gen o $SHORT > "$w/o_short"; gen o $LONG > "$w/o_long"
feed() { printf 'motd\n%s\n' "$_SSH_MARK"; if [[ $2 == short ]]; then cat "$1"; else sed '' "$1"; fi; }
for ((n = 1; n <= $3; n++)); do
  if ((n % 2)); then es=short os=long; else es=long os=short; fi
  f=$w/stage.log
  { ( feed "$w/e_$es" $es | _ssh_errfilter >&2 ) &
    feed "$w/o_$os" $os | _ssh_output
    wait; } > "$f" 2>&1
  size=$(( ($(wc -l < "$w/e_$es") + $(wc -l < "$w/o_$os")) * 8 ))
  if [[ $(stat -c %s "$f") != "$size" ]] || ! grep -a '^e' "$f" | cmp -s - "$w/e_$es" ||
     ! grep -a '^o' "$f" | cmp -s - "$w/o_$os"; then
    echo "$n $es $os"; exit 1
  fi
done
'''.replace('$SHORT', str(SHORT)).replace('$LONG', str(LONG))
PIPE = r'''source "$1"
set +e
w=$2
seq 100001 105000 | sed 's/^/e/' > "$w/e"; seq 100001 105000 | sed 's/^/o/' > "$w/o"
for ((n = 1; n <= 50; n++)); do
  { ( { printf 'motd\n%s\n' "$_SSH_MARK"; sed '' "$w/e"; } | _ssh_errfilter >&2 ) &
    { printf 'motd\n%s\n' "$_SSH_MARK"; cat "$w/o"; } | _ssh_output
    wait; } 2>&1 | cat > "$w/stage.log"
  grep -a '^e' "$w/stage.log" | cmp -s - "$w/e" && grep -a '^o' "$w/stage.log" | cmp -s - "$w/o" &&
    [[ $(stat -c %s "$w/stage.log") == 80000 ]] || { echo "$n"; exit 1; }
done
'''
NO_MARK = r'''source "$1"
set +e
printf 'motd\nbefore\ntail' | _ssh_output; echo " rc=$? no_mark=$_SSH_OUTPUT_NO_MARK"
printf 'refused\ntail' | _ssh_errfilter; echo " rc=$?"
'''


def bash(script, *args):
    return subprocess.run(['bash', '-c', script, 'bash', str(ROOT / 'infra/lib/ssh.sh'), *args],
                          text=True, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)


class SshDdNotruncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ssh-dd-notrunc-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_two_dd_in_one_file_keep_every_line(self):
        result = bash(RACE, str(self.dir), str(RUNS))
        if result.returncode:
            n, es, os_ = result.stdout.split()
            data = (self.dir / 'stage.log').read_bytes()
            lines = set(data.decode(errors='replace').splitlines())
            want = {'e': SHORT if es == 'short' else LONG, 'o': SHORT if os_ == 'short' else LONG}
            lost = {s: sum(f'{s}{100000 + i}' not in lines for i in range(1, want[s] + 1)) for s in 'oe'}
            self.fail(f'журнал стадии потерял строки в прогоне {n} из {RUNS} (короткий: '
                      f'{"_ssh_errfilter" if es == "short" else "_ssh_output"}): o {lost["o"]}, '
                      f'e {lost["e"]}, NUL {data.count(0)}, байт {len(data)} из {sum(want.values()) * 8}'
                      + (f'\n{result.stderr[-2000:]}' if result.stderr else ''))
        self.assertEqual(result.stderr, '')

    def test_pipe_keeps_every_line(self):
        result = bash(PIPE, str(self.dir))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''),
                         'в трубу строки потерялись, прогон ' + result.stdout)

    def test_no_mark_keeps_output_and_code(self):
        result = bash(NO_MARK)
        self.assertEqual((result.stdout, result.stderr),
                         ('motd\nbefore\ntail rc=3 no_mark=3\nrefused\ntail rc=0\n', ''))


if __name__ == '__main__':
    unittest.main()
