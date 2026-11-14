"""Сервер на свободных портах: порт, отпущенный до запуска, может занять другой процесс."""
import os
from pathlib import Path
import socket
import time

# Таблица /proc/net и состояние слушающего сокета: TCP LISTEN, UDP без соединения.
TABLES = {socket.SOCK_STREAM: ('tcp', '0A'), socket.SOCK_DGRAM: ('udp', '07')}


def held(pid, kind):
    """Порты, которые слушают сокеты самого процесса pid."""
    inodes = set()
    for fd in Path(f'/proc/{pid}/fd').glob('*'):
        try:
            link = os.readlink(fd)
        except OSError:
            continue
        if link.startswith('socket:['):
            inodes.add(link[8:-1])
    table, state = TABLES[kind]
    ports = set()
    for name in (table, table + '6'):
        for line in Path('/proc/net', name).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == state and fields[9] in inodes:
                ports.add(int(fields[1].rsplit(':', 1)[1], 16))
    return ports


def start_on_free_ports(start, count, kind, log, attempts=5):
    """(процесс, порты): start(*ports) запускает сервер, log() - его вывод.

    Готов, когда процесс сам держит все порты; порт успел занять другой - новые порты и новый запуск.
    """
    for _ in range(attempts):
        sockets = [socket.socket(type=kind) for _ in range(count)]
        for sock in sockets:
            sock.bind(('127.0.0.1', 0))
        ports = [sock.getsockname()[1] for sock in sockets]
        for sock in sockets:
            sock.close()
        process = start(*ports)
        deadline = time.monotonic() + 20
        while process.poll() is None and not set(ports) <= held(process.pid, kind):
            assert time.monotonic() < deadline, f'сервер не занял порты {ports}:\n{log()}'
            time.sleep(.05)
        if process.poll() is None:
            return process, ports
        assert 'address already in use' in log(), 'сервер вышел:\n' + log()
    raise AssertionError(f'порты {attempts} раз подряд заняты другими:\n{log()}')
