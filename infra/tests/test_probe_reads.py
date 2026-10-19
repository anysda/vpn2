"""Live probes read whole replies: a target may send headers and body apart."""

import ast
import contextlib
import ipaddress
from pathlib import Path
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

import probe_io

TESTS = Path(__file__).resolve().parent
PAUSE = .15


def probe_functions(name):
    """Protocol functions of a probe script, without its netns fixture."""
    path = TESTS / name
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    scope = {'socket': socket, 'struct': struct, 'ipaddress': ipaddress, **vars(probe_io)}
    exec(compile(tree, str(path), 'exec'), scope)
    return scope


HEADERS = b'HTTP/1.0 200 OK\r\nServer: fixture\r\n\r\n'
BODY = b'target reached'


@contextlib.contextmanager
def split_target(*replies):
    """Reads one request per reply and sends the reply's pieces with a pause between."""
    listener = socket.create_server(('127.0.0.1', 0))
    port = listener.getsockname()[1]

    def serve():
        connection, _ = listener.accept()
        with connection:
            for pieces in replies:
                connection.recv(4096)
                for number, piece in enumerate(pieces):
                    if number:
                        time.sleep(PAUSE)
                    connection.sendall(piece)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        listener.close()
        thread.join(5)


class SplitReplyTests(unittest.TestCase):
    def exit_failure_request(self, *replies):
        scope = probe_functions('probe_exit_failure.py')
        with split_target(*replies) as port:
            connect = socket.create_connection
            with patch.object(socket, 'create_connection',
                              lambda _address, *a, **k: connect(('127.0.0.1', port), *a, **k)):
                return scope['request']()

    def test_exit_failure_headers_and_body_sent_apart(self):
        # The echo target's real shape: whole SOCKS replies, HTTP headers then body.
        self.assertTrue(self.exit_failure_request(
            [b'\x05\x00'], [b'\x05\x00\x00\x01' + bytes(6)], [HEADERS, BODY]))

    def test_exit_failure_every_field_split(self):
        self.assertTrue(self.exit_failure_request(
            [b'\x05', b'\x00'], [b'\x05\x00\x00', b'\x01' + bytes(6)], [HEADERS[:11], HEADERS[11:], BODY]))

    def test_read_to_close_waits_for_body_after_headers(self):
        with split_target([b'HTTP/1.1 301 Moved Permanently\r\nLocation: /\r\n\r\n', b'moved']) as port:
            with socket.create_connection(('127.0.0.1', port)) as sock:
                sock.sendall(b'GET / HTTP/1.1\r\n\r\n')
                reply = probe_io.read_to_close(sock, 'redirect', 2)
        self.assertTrue(reply.startswith(b'HTTP/1.1 301 '))
        self.assertTrue(reply.endswith(b'moved'))

    def test_stalled_reply_names_the_step(self):
        left, right = socket.socketpair()
        with left, right:
            right.sendall(b'bef')
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, r'TCP echo before restart: 3 bytes in 0.3 s'):
                probe_io.read_exact(left, 6, 'TCP echo before restart', .3)
            self.assertLess(time.monotonic() - started, 2)
            right.close()
            with self.assertRaisesRegex(ConnectionError, r'SOCKS reply: closed after 0 of 4 bytes'):
                probe_io.read_exact(left, 4, 'SOCKS reply', .3)


class SingleRecvTests(unittest.TestCase):
    def test_no_probe_takes_one_stream_recv_for_a_whole_reply(self):
        # Allowed: server handlers (self.request), streaming loops, EOF checks
        # against b'', and argument-less pipe recv(). Everything else goes
        # through probe_io, which reads to full length or to close.
        found = []
        for path in sorted(TESTS.glob('probe_*.py')):
            if path.name == 'probe_io.py':
                continue
            tree = ast.parse(path.read_text())
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'recv' and node.args):
                    continue
                if ast.unparse(node.func.value) == 'self.request':
                    continue
                parent, looped = parents.get(node), False
                while parent is not None:
                    if isinstance(parent, ast.While):
                        looped = True
                        break
                    parent = parents.get(parent)
                compare = parents.get(node)
                eof_check = (isinstance(compare, ast.Compare) and len(compare.comparators) == 1
                             and isinstance(compare.comparators[0], ast.Constant)
                             and compare.comparators[0].value == b'')
                if not looped and not eof_check:
                    found.append(f'{path.name}:{node.lineno} {ast.unparse(node)}')
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
