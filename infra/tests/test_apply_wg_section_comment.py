"""wg section lines with a comment or blanks: wg0.conf split and syncconf body read as wg(8) reads them.

wg cuts a line at the first #, drops every space character in it and only then
compares it with [Interface] or [Peer] in any ASCII case (wireguard-tools
config.c, config_read_line and process_line). "[Peer] # bob", "[Peer]#x" and
"[ peer ]" each start a peer to wg.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_apply import HEADER, KEY, ROOT, apply, peer
from test_apply_wg_keys import Host
from test_control import module

SPACE = ' \t\n\v\f\r'
INTERFACE = ('interface', [('listenport', '51820'), ('privatekey', KEY['s'])])


def peers(*names):
    return [('peer', [('publickey', KEY[name]), ('allowedips', '10.66.66.2/32')]) for name in names]


def wg_read(body):
    """Sections and keys of a syncconf body in the order wg reads them."""
    sections = []
    for line in body.split('\n'):
        seen = ''.join(char for char in line.split('#')[0] if char not in SPACE)
        if seen.lower() in ('[interface]', '[peer]'):
            sections.append((seen.lower()[1:-1], []))
        elif seen:
            if not sections:
                raise AssertionError(f'wg: key outside a section: {line!a}')
            name, _, value = seen.partition('=')
            sections[-1][1].append((name.lower(), value))
    return sections


def section(name, line, text):
    """text with the [Peer] line of peer name replaced by line."""
    return text.replace(f'[Peer]\nPublicKey = {KEY[name]}', f'{line}\nPublicKey = {KEY[name]}')


class SectionCommentTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        (self.work / 'state').mkdir()
        self.host = Host(self.work)
        self.applier = apply.Applier(self.work / 'root', self.work / 'state', self.host)
        self.applier.settle = 0
        self.path = self.work / 'root/etc/wireguard/wg0.conf'
        self.path.parent.mkdir(parents=True)
        self.new = self.work / 'new.conf'

    def synced(self):
        return [data for argv, data in self.host.calls if argv[:2] == ['/usr/bin/wg', 'syncconf']]

    def install(self, current, candidate=HEADER):
        """Installs candidate over a wg0.conf holding current; the Failed or None."""
        self.path.write_text(current)
        self.host.calls.clear()
        self.new.write_text(candidate)
        try:
            self.applier.install('wg', self.new)
        except apply.Failed as error:
            return error
        return None

    def applied(self, current, candidate=HEADER):
        """The syncconf body of an install that must pass, as wg reads it."""
        error = self.install(current, candidate)
        if error:
            self.fail(f'install failed (code {error.code}): {error}')
        self.assertEqual(len(self.synced()), 1)
        return wg_read(self.synced()[0])

    def test_second_peer_with_a_comment_is_its_own_peer(self):
        current = HEADER + peer('a') + section('b', '[Peer] # bob', peer('b'))
        self.assertEqual(self.applied(current), [INTERFACE] + peers('a', 'b'))

    def test_peer_line_as_wg_reads_it_is_a_peer_or_rejected(self):
        # config.c: "[Peer]#x" -> "[Peer]", "\t[Peer]\t" -> "[Peer]", "[ Peer ]" -> "[Peer]",
        # "[ peer ] # c" -> "[peer]": each strcasecmp-equal to "[Peer]", a new peer.
        for line in ('[Peer] # bob', '[Peer]#x', '\t[Peer]\t', '[ Peer ]', '[ peer ] # c'):
            for name in ('a', 'b'):
                current = section(name, line, HEADER + peer('a') + peer('b'))
                with self.subTest(line=line, peer=name):
                    error = self.install(current)
                    if error:
                        self.assertEqual(error.code, 2, str(error))
                        self.assertIn('candidate rejected, working config untouched', str(error))
                        self.assertEqual(self.synced(), [])
                        self.assertEqual(self.path.read_text(), current)
                    else:
                        self.assertEqual(wg_read(self.synced()[0]), [INTERFACE] + peers('a', 'b'))

    def test_split_cuts_at_a_first_peer_with_a_comment(self):
        tail = section('a', '[Peer] # alice', peer('a')) + peer('b')
        header, rest = apply.wg_split(HEADER + tail)
        self.assertEqual(header, HEADER)
        self.assertIn(KEY['a'], rest)
        self.assertIn(KEY['b'], rest)

    def test_interface_reapplied_keeps_a_first_peer_with_a_comment(self):
        tail = section('a', '[Peer] # alice', peer('a')) + peer('b')
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                self.assertEqual(self.applied(self.path.read_text() if attempt == 2 else HEADER + tail),
                                 [INTERFACE] + peers('a', 'b'))
                written = self.path.read_text()
                self.assertEqual(apply.wg_split(written)[0], HEADER)
                for name in 'ab':
                    self.assertEqual(written.count(KEY[name]), 1, written)

    def test_interface_with_a_comment_reaches_syncconf(self):
        header = HEADER.replace('[Interface]', '[Interface] # main')
        self.assertEqual(self.applied(header + peer('a'), header), [INTERFACE] + peers('a'))

    def test_comments_after_a_key_and_on_a_whole_line(self):
        current = HEADER + peer('a').replace(f'PublicKey = {KEY["a"]}', f'PublicKey = {KEY["a"]} # c') + \
            '# [Peer]\n' + peer('b')
        self.assertEqual(self.applied(current), [INTERFACE] + peers('a', 'b'))

    def test_plain_config_body_is_unchanged(self):
        self.assertEqual(self.applied(HEADER + peer('a') + peer('b')), [INTERFACE] + peers('a', 'b'))
        self.assertEqual(self.synced()[0],
                         f'[Interface]\nListenPort = 51820\nPrivateKey = {KEY["s"]}\n'
                         f'[Peer]\nPublicKey = {KEY["a"]}\nAllowedIPs = 10.66.66.2/32\n'
                         f'[Peer]\nPublicKey = {KEY["b"]}\nAllowedIPs = 10.66.66.2/32\n')


class BackendSectionCommentTests(unittest.TestCase):
    def setUp(self):
        module('contract')
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        (self.root / 'state').mkdir()
        self.backend = module('backend').Backend(self.root, self.root / 'state')
        self.backend.pools = json.loads((ROOT / 'app/shared/vpn-pools.json').read_text())
        self.path = self.root / 'etc/wireguard/wg0.conf'
        self.path.parent.mkdir(parents=True)
        self.peer = {'key': KEY['a'], 'preshared_key': KEY['c'], 'ip': '10.66.66.2'}
        self.synced = []

    def command(self, *args, data=None):
        if args[1] == 'syncconf':
            self.synced.append(data)
            return ''
        if args[-1] == 'preshared-keys':
            return f'{KEY["a"]}\t{KEY["c"]}\n'
        return f'{KEY["a"]}\t10.66.66.2/32 fd66:66::2/128\n'

    def applied(self, previous):
        self.synced.clear()
        self.path.write_text(previous)
        with patch.object(self.backend, 'command', side_effect=self.command):
            self.backend.wireguard({'version': 1, 'peers': [self.peer]})
        return wg_read(self.synced[0])

    def test_old_peer_with_a_comment_is_replaced(self):
        for old in (section('b', '[Peer] # bob', peer('b')),
                    peer('c') + section('b', '[Peer] # bob', peer('b'))):
            with self.subTest(old=old):
                sections = self.applied(HEADER + old)
                self.assertEqual([name for name, _ in sections], ['interface', 'peer'])
                self.assertEqual(sections[1][1][0], ('publickey', KEY['a']))
                written = self.path.read_text()
                self.assertNotIn(KEY['b'], written)
                self.assertIn('Address = 10.66.66.1/24', written)
                self.assertEqual(wg_read(written.replace('Address = 10.66.66.1/24\n', ''))[0], INTERFACE)

    def test_comments_after_a_key_and_on_a_whole_line(self):
        previous = HEADER.replace(f'PrivateKey = {KEY["s"]}', f'PrivateKey = {KEY["s"]} # server') \
            .replace('ListenPort', '# [Peer]\nListenPort') + \
            peer('b').replace(f'PublicKey = {KEY["b"]}', f'PublicKey = {KEY["b"]} # old')
        sections = self.applied(previous)
        self.assertEqual(sections[0], INTERFACE)
        written = self.path.read_text()
        self.assertIn('ListenPort = 51820', written)
        self.assertNotIn(KEY['b'], written)


if __name__ == '__main__':
    unittest.main()
