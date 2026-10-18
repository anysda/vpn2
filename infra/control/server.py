"""Socket-activated VPN control service. All installed sources are root-owned."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time

from backend import Backend
from contract import API, Fault, LIMIT, load_pools, require, validate


def object_pairs(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result)
        result[name] = value
    return result


def handle(connection, backend, allowed_uids, pools):
    connection.settimeout(3)
    try:
        _pid, uid, _gid = struct.unpack('iII', connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('iII')))
        require(uid in allowed_uids, 'forbidden')
        buffer = bytearray()
        deadline = time.monotonic() + 3
        while b'\n' not in buffer:
            require(time.monotonic() < deadline, 'limit')
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            chunk = connection.recv(min(8192, LIMIT + 1 - len(buffer)))
            require(bool(chunk))
            buffer.extend(chunk)
            require(len(buffer) <= LIMIT, 'limit')
        line, suffix = buffer.split(b'\n', 1)
        require(not suffix)
        request = json.loads(line, object_pairs_hook=object_pairs)
        method, params = validate(request, pools)
        response = {'api': API, 'ok': True, 'result': backend.execute(method, params)}
        encoded = json.dumps(response).encode() + b'\n'
        require(len(encoded) <= LIMIT, 'limit')
    except Fault as error:
        encoded = json.dumps({'api': API, 'ok': False, 'error': str(error)}).encode() + b'\n'
    except (ValueError, TypeError, RecursionError):
        encoded = b'{"api":1,"ok":false,"error":"validation"}\n'
    except (OSError, KeyError, subprocess.TimeoutExpired):
        encoded = b'{"api":1,"ok":false,"error":"unavailable"}\n'
    try:
        connection.sendall(encoded)
    except OSError:
        pass


def serve(listener, backend, allowed_uids, pools):
    while True:
        connection, _address = listener.accept()
        with connection:
            handle(connection, backend, allowed_uids, pools)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    if args.probe:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(5)
            connection.connect('/run/vpn2-control/control.sock')
            connection.sendall(b'{"api":1,"method":"check.run","params":{"check":"readiness"}}\n')
            reply = json.loads(connection.makefile('rb').readline(LIMIT))
            require(reply.get('ok') is True, 'unavailable')
            print('vpn2-control: API 1 ready')
        return
    require(os.environ.get('LISTEN_PID') == str(os.getpid()) and
            os.environ.get('LISTEN_FDS') == '1', 'unavailable')
    config = json.loads(Path('/etc/vpn2-control/callers.json').read_text())
    require(type(config) is list and all(type(uid) is int and uid >= 0 for uid in config))
    backend = Backend()
    with (backend.state / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with socket.socket(fileno=3) as listener:
            require(listener.family == socket.AF_UNIX)
            serve(listener, backend, set(config), load_pools())


if __name__ == '__main__':
    main()
