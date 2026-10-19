"""Socket-activated VPN control service. All installed sources are root-owned."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

from backend import Backend
from contract import API, Fault, LIMIT, load_pools, require, validate


def object_pairs(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result)
        result[name] = value
    return result


def receive(connection):
    buffer = bytearray()
    deadline = time.monotonic() + 3
    while b'\n' not in buffer:
        require(time.monotonic() < deadline, 'limit')
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        chunk = connection.recv(min(8192, LIMIT + 1 - len(buffer)))
        require(bool(chunk))
        buffer.extend(chunk)
        require(len(buffer) <= LIMIT, 'limit')
    return buffer


def handle(connection, backend, allowed_uids, pools):
    connection.settimeout(3)
    try:
        _pid, uid, _gid = struct.unpack('iII', connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('iII')))
        if uid not in allowed_uids:
            # Read the request before refusing: closing a socket with unread
            # data resets it, and the caller saw a broken pipe, not a reply.
            try:
                receive(connection)
            except (Fault, OSError):
                pass
            print(f'vpn2-control: forbidden caller uid={uid}', file=sys.stderr, flush=True)
            raise Fault('forbidden')
        line, suffix = receive(connection).split(b'\n', 1)
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
        connection.shutdown(socket.SHUT_WR)
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
    parser.add_argument('--expire', action='store_true')
    args = parser.parse_args()
    if args.probe or args.expire:
        # Through the socket: the one server process serializes all changes.
        request = {'method': 'expiry.run', 'params': {}} if args.expire else \
            {'method': 'check.run', 'params': {'check': 'readiness'}}
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(40 if args.expire else 5)
            connection.connect('/run/vpn2-control/control.sock')
            connection.sendall(json.dumps(dict(request, api=API)).encode() + b'\n')
            reply = json.loads(connection.makefile('rb').readline(LIMIT))
        require(reply.get('ok') is True, 'unavailable')
        if args.probe:
            print('vpn2-control: API 1 ready')
            return
        result = reply['result']
        for item in result['latest']:
            print(f"vpn2-control: expired {item['protocol']} {item['id']} at {item['expires_at']}")
        for protocol in result['failed']:
            print(f'vpn2-control: expiry not enforced for {protocol}', file=sys.stderr)
        require(not result['failed'], 'unavailable')
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
