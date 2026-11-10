"""Вход на экзит через hy2-mgmt под общим пределом времени.

Login shell экзита читает .bashrc до команды, и его вывод не должен попасть в
лог deploy: команда печатает метку, вывод до неё отбрасывается. Без метки shell
узла не дошёл до команды (exit или exec в .bashrc) - это отказ, даже при коде
0; при 255 (соединение) - сообщение ssh. Метка и причина те же, что у
_SSH_MARK и _SSH_NO_MARK_REASON в infra/lib/ssh.sh.

ConnectTimeout ограничивает только соединение: зависший .bashrc или команда
держали бы verify без предела, поэтому весь вход идёт в своей сессии, и по
пределу гибнет вся сессия, а не один ssh.
"""
import contextlib
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from vpn2.verify.host import code

MARK = 'vpn2-ssh-output-begin'
NO_MARK_REASON = 'shell узла не дошёл до команды (.bashrc root делает exit или exec), команда не выполнена'


def processes():
    """Снимок /proc: pid -> (родитель, сессия); зомби не в счёт."""
    table = {}
    for stat in Path('/proc').glob('[0-9]*/stat'):
        with contextlib.suppress(OSError, IndexError, ValueError):
            fields = stat.read_text().rpartition(') ')[2].split()
            if fields[0] != 'Z':
                table[int(stat.parent.name)] = (int(fields[1]), int(fields[3]))
    return table


def signal_all(pids, number):
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, number)


def kill_tree(root):
    """KILL команде, её сессии и всем потомкам, даже отвязавшимся в свою группу или сессию.

    Дерево сперва замирает (STOP: новых детей не наплодит), проходы идут, пока
    находятся новые потомки; убитым корень остаётся последним - его ждёт limited.
    """
    tree = [root]
    signal_all(tree, signal.SIGSTOP)
    while True:
        known = set(tree)
        found = [pid for pid, (parent, session) in processes().items()
                 if pid not in known and (parent in known or session == root)]
        if not found:
            break
        signal_all(found, signal.SIGSTOP)
        tree += found
    signal_all(reversed(tree), signal.SIGKILL)


def limited(argv, limit):
    """Команда в своей сессии не дольше limit с: (код, stdout, stderr, вышел ли предел).

    Вывод идёт в файлы: осиротевший потомок с открытым каналом не держит чтение.
    Лидер не пожат, пока дерево не добито: его номер ядро никому не отдаст.
    Когда команда кончилась сама, гибнет всё, что осталось в её сессии.
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        deadline = time.monotonic() + limit
        expired = False
        try:
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                if time.monotonic() >= deadline:
                    expired = True
                    break
                time.sleep(0.02)
        finally:
            kill_tree(process.pid)
            rc = code(process.wait())
        texts = []
        for stream in (out, err):
            stream.seek(0)
            texts.append(stream.read().decode(errors='replace'))
    return rc, texts[0], texts[1], expired


def exit_ssh(tag, limit):
    """Проверка входа на экзит: (код, вывод команды после метки или причина отказа)."""
    if not re.fullmatch(r'[1-9][0-9]{0,5}', limit):
        return 1, f"SSH_PROBE_TIMEOUT='{limit}': нужно целое число секунд от 1 до 999999 без ведущего нуля"
    rc, out, err, expired = limited(['ssh', '-n', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', tag,
                                     f"printf '\\n%s\\n' {MARK}"], int(limit))
    # 124/137 отдаёт и сам экзит (exit в .bashrc, код команды): пределом это
    # считается, только если время действительно вышло.
    if expired:
        return rc, f'экзит не ответил за предел {limit} с: вход по ssh или .bashrc root на экзите завис'
    # stderr .bashrc приходит от sshd отдельным каналом: показываем только сообщение ssh о соединении.
    if rc == 255:
        return 255, err.rstrip('\n')
    out = out.rstrip('\n')
    if f'\n{MARK}\n' not in f'\n{out}\n':
        return 1, NO_MARK_REASON
    return rc, out[out.index(MARK) + len(MARK):].removeprefix('\n')
