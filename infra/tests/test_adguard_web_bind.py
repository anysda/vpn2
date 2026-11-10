"""AdGuard web UI stays on loopback after an update over an old install."""

import tempfile
import unittest

import yaml

from adguard_support import Hub


class AdguardWebBindTests(unittest.TestCase):
    def test_old_public_bind_moves_to_loopback_once(self):
        for old in ('0.0.0.0:3001', '[::]:3001', '192.0.2.7:3001', None):
            with self.subTest(old=old), tempfile.TemporaryDirectory() as directory:
                hub = Hub(directory, port=3101, user='admin')
                config = {'dns': {'aaaa_disabled': True}, 'users': [{'name': 'admin', 'password': 'hash'}]}
                if old:
                    config['http'] = {'address': old, 'session_ttl': '720h'}
                hub.config.write_text(yaml.safe_dump(config))
                hub.deploy()
                # Опубликованный конфиг сменился: отпечаток службы сброшен, она перезапустится.
                self.assertTrue(hub.changed)
                after = yaml.safe_load(hub.config.read_text())
                self.assertEqual(after['http']['address'], '127.0.0.1:3101')
                if old:
                    self.assertEqual(after['http']['session_ttl'], '720h')
                self.assertEqual(after['users'], [{'name': 'admin', 'password': 'hash'}])
                text = hub.config.read_text()
                hub.deploy()
                self.assertFalse(hub.changed)
                self.assertEqual(hub.config.read_text(), text)


if __name__ == '__main__':
    unittest.main()
