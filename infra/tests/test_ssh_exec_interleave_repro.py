"""ssh_exec в один файл с stderr: строка одного потока не рвётся строкой другого.

deploy пишет стадию в журнал `ssh_exec ... >"$log" 2>&1`: stdout узла в журнал
пишет _ssh_output, stderr - _ssh_errfilter. ssh отдаёт поток кусками по
пакетам канала, и кусок может кончиться посреди строки. Если писатель кладёт
в журнал кусок как есть, строка второго потока встаёт в середину: в checks
42524193514 из `e7765` вышло `e776o4583` и `5`.

Подставной ssh исполняет команду узла через bash, stdout и stderr - разными
каналами, как настоящий. Окно склейки управляемое, а не по таймингу: узел
пишет в один поток начало строки без перевода, ждёт, пока журнал получит его
(или не получит за WAIT с), пишет целую строку во второй поток и ждёт её в
журнале, потом дописывает первую строку. Писатель целыми строками держит
начало у себя, и в журнале обе строки целые.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
WAIT = 3
FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
exec bash -c "$*"
'''
FAKE_SSHPASS = '#!/bin/bash\nshift 2\nexec ssh "$@"\n'
# Узел: ждёт текст в журнале, не дольше $1 с; код 0 - дождался.
SEEN = ('seen() { local i; for ((i = 0; i < $1 * 100; i++)); do '
        'grep -qF -- "$2" "$FAKE_LOG" && return 0; sleep 0.01; done; return 1; }; ')
# Обрывок первой строки, целая строка второго потока, остаток первой.
CASES = {
    'stderr рвётся stdout': ('e77', 'o1', '5', '>&2', '', 'o1\ne775\n'),
    'stdout рвётся stderr': ('o77', 'e1', '5', '', '>&2', 'e1\no775\n'),
}


class SshExecInterleaveTests(unittest.TestCase):
    def run_case(self, head, other, tail, first, second, tag):
        with tempfile.TemporaryDirectory(prefix='ssh-interleave-') as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            for name, text in (('ssh', FAKE_SSH), ('sshpass', FAKE_SSHPASS)):
                (root / 'bin' / name).write_text(text)
                (root / 'bin' / name).chmod(0o755)
            log = root / 'stage.log'
            node = (SEEN + f"printf %s {head} {first}; seen {WAIT} {head} || :; "
                    f'printf "%s\\n" {other} {second}; seen 10 {other} || exit 9; '
                    f'printf "%s\\n" {tail} {first}; exit 7')
            script = f'''DEPLOY_ROOT={root}
source {ROOT}/infra/lib/ssh.sh
SSH_USER=root SSH_HOST=node.example SSH_PASS=fixture SSH_PROBE_TIMEOUT=10
rc=0; ssh_exec '{node}' >"$FAKE_LOG" 2>&1 || rc=$?
echo "rc=$rc"
'''
            env = {**os.environ, 'PATH': f'{root}/bin:{os.environ["PATH"]}',
                   'FAKE_LOG': str(log), 'HOST_TAG': tag}
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=root, env=env,
                                    text=True, capture_output=True, timeout=60,
                                    stdin=subprocess.DEVNULL)
            return result, log.read_text() if log.exists() else ''

    def test_line_of_one_stream_is_not_split_by_other(self):
        for name, (head, other, tail, first, second, want) in CASES.items():
            for tag in ('ru', 'us'):
                with self.subTest(case=name, tag=tag):
                    result, text = self.run_case(head, other, tail, first, second, tag)
                    self.assertEqual((result.returncode, result.stdout), (0, 'rc=7\n'), result.stderr)
                    self.assertEqual(text.splitlines(), want.splitlines(),
                                     f'журнал стадии: строка {head}{tail} разорвана строкой {other}')
                    self.assertEqual(text, want)


if __name__ == '__main__':
    unittest.main()
