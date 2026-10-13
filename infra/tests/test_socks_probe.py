"""A TCP stream may split every SOCKS/HTTP field, even on loopback."""

import ast
import ipaddress
import os
from pathlib import Path
import socket
import struct
import unittest
from unittest.mock import MagicMock, patch


class SocksProbeTests(unittest.TestCase):
    def test_fragmented_reply(self):
        root = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
        path = root / 'infra/tests/probe_exit_loopback.py'
        tree = ast.parse(path.read_text())
        # Load the actual protocol functions without starting the netns fixture.
        tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        scope = {'socket': socket, 'struct': struct, 'ipaddress': ipaddress}
        exec(compile(tree, str(path), 'exec'), scope)
        stream = MagicMock()
        stream.__enter__.return_value = stream
        stream.recv.side_effect = [b'\x05', b'\x00', b'\x05\x00', b'\x00\x01',
                                   b'\x00' * 4, b'\x00' * 2, b'HT', b'TP/']
        with patch.object(socket, 'create_connection', return_value=stream):
            self.assertTrue(scope['tcp_request'](17897, '192.0.2.20', 9100))


if __name__ == '__main__':
    unittest.main()
