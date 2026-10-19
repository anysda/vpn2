"""Stream reads for the live probes.

A TCP peer may deliver headers and body (or any SOCKS field) in separate
segments, so a single recv() can return only part of a reply. Every read here
waits for the whole reply under its own short deadline and names the step
that stalled or closed early. A UDP datagram arrives whole: recv_datagram only
adds the deadline and the name.
"""

import socket
import time


def _recv(sock, size, what, deadline, seconds, have):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f'{what}: {have} bytes in {seconds} s, reply incomplete')
    sock.settimeout(remaining)
    try:
        return sock.recv(size)
    except socket.timeout:
        raise TimeoutError(f'{what}: {have} bytes in {seconds} s, reply incomplete') from None


def read_exact(sock, size, what='reply', seconds=5):
    previous = sock.gettimeout()
    deadline = time.monotonic() + seconds
    data = bytearray()
    try:
        while len(data) < size:
            chunk = _recv(sock, size - len(data), what, deadline, seconds, len(data))
            if not chunk:
                raise ConnectionError(f'{what}: closed after {len(data)} of {size} bytes')
            data += chunk
    finally:
        sock.settimeout(previous)
    return bytes(data)


def read_to_close(sock, what='reply', seconds=5, limit=1 << 20):
    previous = sock.gettimeout()
    deadline = time.monotonic() + seconds
    data = bytearray()
    try:
        while chunk := _recv(sock, 65536, what, deadline, seconds, len(data)):
            data += chunk
            if len(data) > limit:
                raise ValueError(f'{what}: more than {limit} bytes')
    finally:
        sock.settimeout(previous)
    return bytes(data)


def recv_datagram(sock, what='datagram', seconds=3):
    previous = sock.gettimeout()
    sock.settimeout(seconds)
    try:
        return sock.recv(65536)
    except socket.timeout:
        raise TimeoutError(f'{what}: no datagram in {seconds} s') from None
    finally:
        sock.settimeout(previous)
