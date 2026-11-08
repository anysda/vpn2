"""Замок deploy на entry отпускается, когда сессия sshd замка осиротела.

Deploy кончился (FATAL), а ssh до entry закрыться не смог: связь оборвалась
посреди deploy, клиент ssh умер, но серверная сессия жива и её stdin не
получает EOF - sshd с clientaliveinterval 0 держит её до TCP keepalive (~2 ч).
Подставной ssh моделирует это так: команда замка идёт в отдельной сессии
(setsid), её stdin - fifo, который держит открытым посторонний процесс, как
sshd держит живую TCP-сессию. Клиент только пересылает stdin и stdout; обрыв
связи - смерть процессов клиента, и строка отпускания замка до entry уже не
доходит. Тест смотрит на stderr следующего deploy - то, что видит оператор.
"""
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest

from test_deploy import ROOT
from test_deploy_lock_ssh_failure import Entry, START, verdict

# Окно, за которое замок мёртвой сессии обязан освободиться. Оператор
# перезапускает deploy сразу после FATAL; десятки секунд он подождёт, два часа
# TCP keepalive - нет. Окно не опирается на sshd-настройки entry.
RELEASE_WINDOW = 20
# Живой deploy держит замок дольше окна: замок отпускается по смерти клиента,
# а не по возрасту.
LIVE_HOLD = RELEASE_WINDOW + 5
# Посторонний держатель stdin сессии живёт дольше любого прогона теста.
SESSION_TTL = 300

FAKE_SSH = r'''#!/bin/bash
while (($#)); do
  case $1 in -[oiFpl]) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
cmd="$*"
cmd=${cmd//\/var\/lib\/vpn2-deploy/$FAKE_NODE/lock}
[[ $cmd == *deploy.lock* ]] || exec bash -c "$cmd"
s=$(mktemp -d "$FAKE_NODE/session.XXXXXX")
mkfifo "$s/in" "$s/out"
# Сессия sshd: stdin держит открытым посторонний процесс, EOF не придёт.
setsid sleep "$FAKE_SESSION_TTL" 3<>"$s/in" </dev/null >/dev/null 2>&1 &
holder=$!
# Клиент открывает вывод сессии до старта команды: занятый замок flock
# отвергает сразу, и открытие fifo после ухода последнего писателя ждало бы
# нового писателя вечно. Временный писатель 4 нужен, чтобы открыть читателя 5
# без блокировки.
exec 4<>"$s/out" 5<"$s/out"
setsid bash -c "$cmd" <"$s/in" 1<>"$s/out" 2>"$s/err" 4>&- 5<&- &
server=$!
exec 4>&-
echo "$holder $server" > "$s/session"
# Клиент: пересылка stdin на entry и stdout с entry.
exec 3<&0
cat <&3 >"$s/in" 3<&- 5<&- &
forward=$!
exec 3<&-
cat <&5 5<&- &
relay=$!
exec 5<&-
echo "$$ $forward $relay" > "$s/client"
wait "$relay"
rc=0
wait "$server" || rc=$?
kill "$forward" 2>/dev/null
exit "$rc"
'''

# Обрыв связи посреди deploy: процессы клиента ssh замка умирают.
CUT = '''cut_connection() {
  local f
  for f in "$FAKE_NODE"/session.*/client; do kill $(<"$f") 2>/dev/null || true; done
}
'''
FIRST = '''release_start rollback
echo deploy-locked
: > "$FAKE_NODE/locked"
while [[ ! -e $FAKE_NODE/go ]]; do command sleep 0.2; done
cut_connection
die 'stage failed: entry refuses ssh'
'''
NEXT = 'release_start rollback\necho deploy-locked\n'


def kill_tree(root):
    """KILL процессу и всем потомкам, в какой бы группе и сессии они ни были.

    Дерево сначала замирает (STOP), чтобы никто не успел породить нового
    ребёнка или осиротеть, пока его обходят.
    """
    found, todo = [], [root]
    while todo:
        pid = todo.pop()
        try:
            os.kill(pid, signal.SIGSTOP)
        except ProcessLookupError:
            continue
        found.append(pid)
        for children in Path(f'/proc/{pid}/task').glob('*/children'):
            try:
                todo += [int(child) for child in children.read_text().split()]
            except OSError:
                pass
    for pid in found:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class OrphanEntry(Entry):
    def __init__(self):
        super().__init__()
        (self.root / 'bin' / 'ssh').write_text(FAKE_SSH)
        self.env['FAKE_SESSION_TTL'] = str(SESSION_TTL)
        self.node = self.root / 'node'

    def start(self, body):
        exports = ''.join(f"export {k}='{v}'\n" for k, v in self.env.items())
        script = exports + f'''DEPLOY_ROOT=$PWD
source {ROOT}/infra/lib/ssh.sh
sleep() {{ :; }}
SSH_USER=root SSH_HOST=entry.example SSH_PASS=fixture SSH_PROBE_TIMEOUT=5
''' + START + CUT + body
        return subprocess.Popen(['bash', '-euo', 'pipefail', '-c', script],
                                cwd=self.directory.name, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def run(self, body, timeout=15):
        proc = self.start(body)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Вывод шага держат и процессы клиента подставного ssh, не только
            # bash, а ssh.sh уводит их в свою группу: убить всё дерево шага,
            # иначе communicate ждёт EOF вечно.
            kill_tree(proc.pid)
            proc.communicate(timeout=5)
            raise
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def sessions(self):
        for path in self.node.glob('session.*/session'):
            yield [int(pid) for pid in path.read_text().split()]

    def __exit__(self, *_):
        for pids in self.sessions():
            for pid in pids:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        super().__exit__()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Зомби держателя тоже считается мёртвым, пожатый до чтения stat - тоже.
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat.rsplit(')', 1)[1].split()[0] != 'Z'


def wait_for(path, timeout):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


class DeployLockOrphanSessionTests(unittest.TestCase):
    def test_lock_of_orphaned_entry_session_is_released_after_fatal(self):
        with OrphanEntry() as entry:
            first = entry.start(FIRST)
            try:
                if not wait_for(entry.node / 'locked', 15):
                    first.kill()
                    self.fail('первый deploy не взял замок:\n' + first.communicate()[1])
                orphan = list(entry.sessions())
                self.assertEqual(len(orphan), 1, orphan)
                # Контроль: живой deploy держит замок и после окна.
                time.sleep(LIVE_HOLD)
                held = entry.run(NEXT)
                self.assertNotEqual(held.returncode, 0, held.stderr)
                self.assertNotIn('deploy-locked', held.stdout)
                self.assertIn('deploy already running', verdict(held.stderr), held.stderr)
                (entry.node / 'go').touch()
                out, err = first.communicate(timeout=15)
            finally:
                if first.poll() is None:
                    first.kill()
            self.assertEqual(first.returncode, 1, err)
            self.assertIn('FATAL: stage failed', err)
            self.assertIn('deploy-locked', out)
            # Сценарий воспроизведён: клиент deploy мёртв, stdin сессии на
            # entry держит посторонний процесс.
            self.assertTrue(alive(orphan[0][0]), 'держатель stdin сессии умер раньше времени')

            deadline = time.monotonic() + RELEASE_WINDOW
            while True:
                result = entry.run(NEXT)
                if result.returncode == 0 or time.monotonic() > deadline:
                    break
                self.assertIn('deploy already running', verdict(result.stderr), result.stderr)
                time.sleep(1)
            self.assertEqual(result.returncode, 0,
                             f'за {RELEASE_WINDOW} с после FATAL замок мёртвой сессии '
                             f'не освободился:\n{result.stderr}')
            self.assertIn('deploy-locked', result.stdout)
            self.assertNotIn('already running', result.stderr)


if __name__ == '__main__':
    unittest.main()
