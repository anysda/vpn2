"""wg candidate check and syncconf filter: names and values read as wg(8) reads them.

wg takes key and section names in any case and FwMark as "off" in any case or
0..0xffffffff, decimal or with a lowercase 0x (wireguard-tools 1.0.20250521).
"""
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import test_apply
from test_apply import HEADER, KEY, ROOT, apply, peer
from test_control import module

PRIVATE = f'PrivateKey = {KEY["s"]}'
PUBLIC = f'PublicKey = {KEY["a"]}'
ALLOWED = 'AllowedIPs = 10.66.66.2/32'
NAMES = {'[interface]': {'privatekey', 'listenport', 'fwmark'},
         '[peer]': {'publickey', 'presharedkey', 'allowedips', 'endpoint', 'persistentkeepalive'}}


class Host(test_apply.Host):
    """The fake host, with wg syncconf reading names in any case, as wg does."""

    def __call__(self, argv, data=None):
        if argv[:2] != ['/usr/bin/wg', 'syncconf']:
            return super().__call__(argv, data)
        self.calls.append((argv, data))
        section = None
        for line in data.split('\n'):
            seen = re.sub(r'[ \t\v\f\r]', '', line).split('#')[0].lower()
            if seen in NAMES:
                section = seen
            elif seen and (section is None or seen.split('=')[0] not in NAMES[section]):
                raise RuntimeError('wg syncconf: Configuration parsing error')
        return ''


class WireguardKeyNameTests(unittest.TestCase):
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

    def applied(self, header, peers):
        """Installs header over a wg0.conf holding peers; returns the syncconf body."""
        self.path.write_text(HEADER + peers)
        self.host.calls.clear()
        self.new.write_text(header)
        self.applier.install('wg', self.new)
        self.assertEqual(len(self.synced()), 1)
        self.assertEqual(self.path.read_text(), header.rstrip('\n') + '\n\n' + peers)
        return self.synced()[0]

    def rejected(self, header, peers):
        working = HEADER + peers
        self.path.write_text(working)
        self.host.calls.clear()
        self.new.write_text(header)
        with self.assertRaises(apply.Failed) as caught:
            self.applier.install('wg', self.new)
        self.assertEqual(caught.exception.code, 2, str(caught.exception))
        self.assertIn('candidate rejected, working config untouched', str(caught.exception))
        self.assertEqual(self.synced(), [])
        self.assertEqual(self.path.read_text(), working)

    def test_peer_keys_in_any_case_reach_syncconf(self):
        for section, public, allowed in (('[Peer]', 'publickey', 'ALLOWEDIPS'),
                                         ('[Peer]', 'PUBLICKEY', 'allowedips'),
                                         ('[Peer]', 'PublicKey', 'ALLOWEDIPS'),
                                         ('[peer]', 'PublicKey', 'AllowedIPs'),
                                         ('[PEER]', 'publicKey', 'allowedIPs')):
            peers = peer('a').replace('[Peer]', section).replace('PublicKey', public).replace(
                'AllowedIPs', allowed) + peer('b')
            with self.subTest(section=section, public=public, allowed=allowed):
                body = self.applied(HEADER, peers).split('\n')
                for line in (section, f'{public} = {KEY["a"]}', f'{allowed} = 10.66.66.2/32',
                             f'PublicKey = {KEY["b"]}'):
                    self.assertIn(line, body)
                self.assertEqual(len([line for line in body if line.lower() == '[peer]']), 2)

    def test_preshared_key_in_any_case_reaches_syncconf(self):
        peers = peer('a').replace(ALLOWED, f'presharedkey = {KEY["c"]}\n{ALLOWED}')
        self.assertIn(f'presharedkey = {KEY["c"]}', self.applied(HEADER, peers).split('\n'))

    def test_interface_keys_in_any_case_pass_and_reach_syncconf(self):
        for interface, private, port in (('[Interface]', 'privatekey', 'listenport'),
                                         ('[Interface]', 'PRIVATEKEY', 'LISTENPORT'),
                                         ('[interface]', 'PrivateKey', 'ListenPort'),
                                         ('[INTERFACE]', 'privateKey', 'listenPort')):
            header = HEADER.replace('[Interface]', interface).replace('PrivateKey', private).replace(
                'ListenPort', port)
            with self.subTest(interface=interface, private=private, port=port):
                self.applier.check('wg', header + peer('a'), self.new)
                body = self.applied(header, peer('a')).split('\n')
                for line in (interface, f'{private} = {KEY["s"]}', f'{port} = 51820'):
                    self.assertIn(line, body)

    def test_endpoint_in_any_case_passes_the_check(self):
        for name in ('Endpoint', 'endpoint', 'ENDPOINT'):
            with self.subTest(name=name):
                self.applied(HEADER, peer('a').replace(ALLOWED, f'{ALLOWED}\n{name} = 192.0.2.1:51820'))

    def test_bad_value_under_any_case_name_is_rejected(self):
        for line in (f'privatekey = {KEY["s"]}x', 'listenport = 5x', 'fwmark = on'):
            with self.subTest(line=line):
                self.rejected(HEADER + line + '\n', peer('a'))
        for line in ('publickey = abc', 'allowedips = 10.66.66.2/32 x'):
            with self.subTest(line=line):
                self.rejected(HEADER, peer('a').replace(ALLOWED, f'{ALLOWED}\n{line}'))

    def test_fwmark_wg_takes_is_applied(self):
        for mark in ('0xffffffff', '0xFFFFFFFF', '4294967295', '0', '0x0', '000123', '0x00ff', 'off'):
            for name in ('FwMark', 'fwmark'):
                with self.subTest(name=name, mark=mark):
                    body = self.applied(HEADER + f'{name} = {mark}\n', peer('a'))
                    self.assertIn(f'{name} = {mark}', body.split('\n'))

    def test_fwmark_wg_refuses_is_rejected_before_syncconf(self):
        # wg: Fwmark is neither 0/off nor 0-0xffffffff
        for mark in ('0x100000000', '4294967296', '18446744073709551616', '0X1F', '0x', '00x1f', '-1', 'on'):
            for name in ('FwMark', 'fwmark', 'FWMARK'):
                with self.subTest(name=name, mark=mark):
                    self.rejected(HEADER + f'{name} = {mark}\n', peer('a'))

    def test_plain_config_syncconf_body_is_unchanged(self):
        self.assertEqual(self.applied(HEADER, peer('a') + peer('b')),
                         f'[Interface]\nListenPort = 51820\n{PRIVATE}\n'
                         f'[Peer]\n{PUBLIC}\n{ALLOWED}\n'
                         f'[Peer]\nPublicKey = {KEY["b"]}\n{ALLOWED}\n')


class BackendKeyNameTests(unittest.TestCase):
    """wg.apply keeps the interface of a wg0.conf apply.py installs, names in any case."""

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

    def test_interface_and_peer_names_in_any_case(self):
        for interface, private, port, section in (('[Interface]', 'privatekey', 'LISTENPORT', '[peer]'),
                                                  ('[interface]', 'PRIVATEKEY', 'listenport', '[PEER]')):
            with self.subTest(interface=interface, private=private, section=section):
                self.synced.clear()
                self.path.write_text(f'{interface}\nAddress = 10.66.66.1/24\n{private} = {KEY["s"]}\n'
                                     f'{port} = 51820\n\n{section}\npublickey = {KEY["b"]}\n'
                                     'allowedips = 10.66.66.9/32\n')
                with patch.object(self.backend, 'command', side_effect=self.command):
                    self.backend.wireguard({'version': 1, 'peers': [self.peer]})
                body = self.synced[0].split('\n')
                self.assertIn(f'{private} = {KEY["s"]}', body)
                self.assertIn(f'{port} = 51820', body)
                written = self.path.read_text()
                self.assertNotIn(KEY['b'], written)
                self.assertIn(f'PublicKey = {KEY["a"]}', written)

    def test_server_key_replaces_a_private_key_in_any_case(self):
        (self.root / 'etc/wireguard/server.priv').write_text(KEY['s'] + '\n')
        self.path.write_text(f'[Interface]\nprivatekey = {KEY["b"]}\nListenPort = 51820\n')
        with patch.object(self.backend, 'command', side_effect=self.command):
            self.backend.wireguard({'version': 1, 'peers': [self.peer]})
        self.assertNotIn(KEY['b'], self.path.read_text() + self.synced[0])
        self.assertIn(f'PrivateKey = {KEY["s"]}', self.synced[0].split('\n'))


if __name__ == '__main__':
    unittest.main()
