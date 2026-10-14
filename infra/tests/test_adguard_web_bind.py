"""AdGuard web UI stays on loopback after an update over an old install."""

from pathlib import Path
import re
import tempfile
import unittest

import yaml

from test_deploy import ROOT, bash, source


def patch(path, port):
    stage = source('infra/scripts/22-adguard.sh')
    function = re.search(r'^patch_agh_config\(\) \{\n.*?^\}', stage, re.M | re.S).group()
    result = bash(f'source "{ROOT}/infra/lib/anysda-svc.sh"\n'
                  f'HOST_TAG=test\nMGMT_IP=192.0.2.53\nAGH_PORT={port}\n'
                  'svc_forget() { echo forget; }\n'
                  + function + f'\npatch_agh_config "{path}"', path.parent)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


class AdguardWebBindTests(unittest.TestCase):
    def test_old_public_bind_moves_to_loopback_once(self):
        for old in ('0.0.0.0:3001', '[::]:3001', '192.0.2.7:3001', None):
            with self.subTest(old=old), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'AdGuardHome.yaml'
                config = {'dns': {'aaaa_disabled': True}, 'users': [{'name': 'admin'}]}
                if old:
                    config['http'] = {'address': old, 'session_ttl': '720h'}
                path.write_text(yaml.safe_dump(config))
                self.assertIn('forget', patch(path, 3101))
                after = yaml.safe_load(path.read_text())
                self.assertEqual(after['http']['address'], '127.0.0.1:3101')
                if old:
                    self.assertEqual(after['http']['session_ttl'], '720h')
                self.assertEqual(after['users'], [{'name': 'admin'}])
                text = path.read_text()
                self.assertNotIn('forget', patch(path, 3101))
                self.assertEqual(path.read_text(), text)


if __name__ == '__main__':
    unittest.main()
