"""wg compares key and section names with strncasecmp(): ASCII only.

Python's re.I also takes U+0130 and U+0131 for i, U+017F for s and U+212A for k;
wg reads such a name as an unknown line ("Line unrecognized").
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_apply import HEADER, KEY, ROOT, apply, peer
from test_apply_wg_keys import Host
from test_control import module

# Each name with one letter wg does not take for it.
LOOKALIKE = {'PrivateKey': ('PrivateKey', 'PrıvateKey', 'PrİvateKey'),
             'ListenPort': ('LiſtenPort', 'LıstenPort'),
             'PublicKey': ('PublicKey', 'PublıcKey'),
             'PresharedKey': ('PreſharedKey', 'PresharedKey'),
             'AllowedIPs': ('AllowedIPſ', 'AllowedİPs')}


class WireguardAsciiNameTests(unittest.TestCase):
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
        self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), ['wg0.conf'])

    def test_interface_key_with_a_lookalike_letter_is_rejected(self):
        for name in ('PrivateKey', 'ListenPort'):
            for lookalike in LOOKALIKE[name]:
                with self.subTest(name=ascii(lookalike)):
                    self.rejected(HEADER.replace(name, lookalike), peer('a'))

    def test_public_key_with_a_lookalike_letter_is_rejected(self):
        for lookalike in LOOKALIKE['PublicKey']:
            with self.subTest(name=ascii(lookalike)):
                self.rejected(HEADER, peer('a').replace('PublicKey', lookalike))

    def test_peer_key_with_a_lookalike_letter_does_not_reach_syncconf(self):
        # wg would refuse the whole body; the filter leaves the line out, as on a name it does not know.
        for name, value in (('PresharedKey', KEY['c']), ('AllowedIPs', '10.66.66.3/32')):
            for lookalike in LOOKALIKE[name]:
                with self.subTest(name=ascii(lookalike)):
                    peers = peer('a').replace('AllowedIPs', f'{lookalike} = {value}\nAllowedIPs')
                    self.path.write_text(HEADER + peers)
                    self.host.calls.clear()
                    self.new.write_text(HEADER)
                    self.applier.install('wg', self.new)
                    self.assertEqual(len(self.synced()), 1)
                    self.assertNotIn(lookalike, self.synced()[0])
                    self.assertEqual(self.synced()[0], self.synced()[0].encode('ascii').decode())


class BackendAsciiNameTests(unittest.TestCase):
    def setUp(self):
        self.fault = module('contract').Fault
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

    def wireguard(self):
        with patch.object(self.backend, 'command', side_effect=self.command):
            self.backend.wireguard({'version': 1, 'peers': [self.peer]})

    def test_private_key_with_a_lookalike_letter_is_not_a_private_key(self):
        for keep in (False, True):
            for lookalike in LOOKALIKE['PrivateKey']:
                with self.subTest(name=ascii(lookalike), server_priv=keep):
                    server = self.root / 'etc/wireguard/server.priv'
                    server.unlink(missing_ok=True)
                    if keep:
                        server.write_text(KEY['s'] + '\n')
                    working = HEADER.replace('PrivateKey', lookalike).replace(KEY['s'], KEY['b'])
                    self.path.write_text(working)
                    self.synced.clear()
                    with self.assertRaises(self.fault) as caught:
                        self.wireguard()
                    self.assertEqual(str(caught.exception), 'unavailable')
                    self.assertEqual(self.synced, [])
                    self.assertEqual(self.path.read_text(), working)

    def test_interface_key_with_a_lookalike_letter_does_not_reach_syncconf(self):
        for lookalike in LOOKALIKE['ListenPort']:
            with self.subTest(name=ascii(lookalike)):
                self.path.write_text(HEADER.replace('ListenPort', lookalike))
                self.synced.clear()
                self.wireguard()
                self.assertNotIn(lookalike, self.synced[0])
                self.assertIn(f'PrivateKey = {KEY["s"]}', self.synced[0].split('\n'))


if __name__ == '__main__':
    unittest.main()
