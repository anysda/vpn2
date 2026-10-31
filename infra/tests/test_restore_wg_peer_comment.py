from pathlib import Path
import os
import re
import tempfile
import unittest

from test_deploy import source

INTERFACE = ['[Interface]', 'PrivateKey = server', 'ListenPort = 51820', '']
PEERS = {name: [f'# device {name}', '[Peer]', f'PublicKey = key{name}', f'AllowedIPs = 10.8.0.{name}/32', '']
         for name in '123'}


def drop_peers(text, keys):
    script = source('infra/lib/anysda-restore.sh')
    block = 'def rewrite(path' + script.split('def rewrite(path', 1)[1].split(
        'if (ike_ids or ike_secrets)', 1)[0]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'wg0.conf'
        path.write_bytes(text.encode('latin-1'))
        exec(block, {'os': os, 're': re, 'wg_keys': set(keys), 'wg_path': str(path)})
        return path.read_bytes().decode('latin-1')


def conf(names, newline, tail=()):
    lines = INTERFACE + [line for name in names for line in PEERS[name]] + list(tail)
    return newline.join(lines) + newline


class RestoreWgPeerCommentTests(unittest.TestCase):
    def test_removed_peer_takes_only_its_own_comment(self):
        cases = [('1',), ('2',), ('3',), ('1', '2'), ('2', '3')]
        for newline in ('\n', '\r\n'):
            for removed in cases:
                with self.subTest(newline=repr(newline), removed=removed):
                    kept = [name for name in '123' if name not in removed]
                    result = drop_peers(conf('123', newline), {f'key{name}' for name in removed})
                    self.assertEqual(result, conf(kept, newline),
                                     'comments above kept peers must stay in place, bytes unchanged')

    def test_comment_at_end_of_file_survives_removal_of_last_peer(self):
        tail = ['# managed by operator']
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=repr(newline)):
                result = drop_peers(conf('123', newline, tail), {'key3'})
                self.assertEqual(result, conf('12', newline, tail))

    def test_line_endings_and_comments_kept_without_matching_peer(self):
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=repr(newline)):
                original = conf('123', newline, ['# tail'])
                self.assertEqual(drop_peers(original, {'unknown'}), original)


if __name__ == '__main__':
    unittest.main()
