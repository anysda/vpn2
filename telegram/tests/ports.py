"""Порт для бота под тестом: ниже эфемерного диапазона и под замком до конца теста.

Порт из bind(0) после закрытия может взять исходящее соединение или параллельный тест.
"""
import fcntl
import os
import socket
import tempfile

FIRST, LAST = 20000, 32767


def event_port(case):
    """Свободный порт, который не выдаётся другим тестам, пока `case` не закончится."""
    start = os.getpid() % (LAST - FIRST)
    for step in range(LAST - FIRST):
        port = FIRST + (start + step) % (LAST - FIRST)
        lock = open(os.path.join(tempfile.gettempdir(), f'vpn2-tgbot-port-{port}.lock'), 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
        except OSError:
            lock.close()
            continue
        case.addCleanup(lock.close)
        return port
    raise RuntimeError('нет свободного порта для бота')
