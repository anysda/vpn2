"""Hysteria2 и служебный канал до экзита идут только с закреплённым сертификатом."""
import json
from pathlib import Path
import tempfile
import unittest

from router_support import ROOT, exit_cert, generate
from test_deploy import bash, function, write_envs


def hy2_tls(cfg):
    return {o['tag']: o['tls'] for o in cfg['outbounds'] if o['type'] == 'hysteria2'}


class ExitPinTests(unittest.TestCase):
    def test_pinned_exits_have_no_insecure_anywhere(self):
        text = generate()
        self.assertNotIn('insecure', text)
        tls = hy2_tls(json.loads(text))
        self.assertTrue(any(tag.endswith('-mgmt') for tag in tls))
        for tag, settings in tls.items():
            pinned = Path(exit_cert()).parent / 'pinned' / f"{tag.removeprefix('transport-').split('-')[1]}.pem"
            self.assertEqual(settings.get('certificate_path'), str(pinned), tag)

    def test_config_flag_reaches_env_and_deploy_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = (ROOT / 'config.example.yaml').read_text().replace(
                '  passphrase: ""', '  passphrase: test pass')
            (root / 'config.yaml').write_text(base.replace(
                '    host: 5.6.7.8\n', '    host: 5.6.7.8\n    hy2_insecure: true\n'))
            result = write_envs(tmp, env={'VPN2_STATE_DIR': f'{tmp}/state'})
            self.assertEqual(result.returncode, 0, result.stderr)
            env = (root / 'infra/envs/all.env').read_text()
            self.assertIn('US_HY2_INSECURE=1\n', env)
            self.assertNotIn('GB_HY2_INSECURE', env)
            script = function('warn_insecure_exits') + '''
ENVS=$PWD/infra/envs HOST_TAG=deploy
exit_tags() { echo 'us gb'; }
warn() { printf '[%s] WARN %s\\n' "$HOST_TAG" "$*"; }
warn_insecure_exits
'''
            result = bash(script, tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('[deploy] WARN us: exits[].hy2_insecure: true', result.stdout)
            self.assertNotIn('gb:', result.stdout)

if __name__ == '__main__':
    unittest.main()
