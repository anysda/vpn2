import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from router_support import ROOT, SB


@unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
class ConfigPublishTests(unittest.TestCase):
    def test_invalid_generation_keeps_previous_config(self):
        for stage in ('20-ru-router', '10-foreign'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                config = directory / ('config-base.json' if stage.startswith('20') else 'config.json')
                config.write_text('{"outbounds":[{"type":"direct","tag":"direct"}]}')
                before = config.read_bytes()
                result = self.publish(stage, directory, '{"invalid_field":true}')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(config.read_bytes(), before)
                self.assertFalse(list(directory.glob('.config*')))

    def test_valid_config_is_published_with_private_permissions(self):
        for stage in ('20-ru-router', '10-foreign'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                body = '{"outbounds":[{"type":"direct","tag":"direct"}]}'
                result = self.publish(stage, directory, body)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                config = directory / ('config-base.json' if stage.startswith('20') else 'config.json')
                self.assertEqual(config.read_text().strip(), body)
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    @staticmethod
    def publish(stage, directory, body):
        source = (ROOT / f'infra/scripts/{stage}.sh').read_text()
        if stage.startswith('20'):
            source = source.split('# Write the BASE config', 1)[1].split('# config.json (база', 1)[0]
            source = '# Write the BASE config' + source
        else:
            source = 'TPL=' + source.split('TPL=', 1)[1].split('cat > /etc/systemd/system/', 1)[0]
        source = source.replace('/etc/sing-box', str(directory))
        source = source.replace('/tmp/anysda/sing-box-server.json.tpl', str(directory / 'template'))
        source = source.replace('/usr/local/bin/sing-box', '"$SING_BOX_BIN"')
        source = source.replace('/tmp/anysda/hy2_bandwidth.py', str(ROOT / 'infra/lib/hy2_bandwidth.py'))
        (directory / 'template').write_text(body)
        generator = directory / 'generate.py'
        generator.write_text(f'print({body!r})\n')
        shell = 'set -euo pipefail\nenvsubst() { command cat; }\n' + source
        return subprocess.run(['bash', '-c', shell], text=True, capture_output=True,
                              env={**os.environ, 'HOST_TAG': 'test', 'GEN': str(generator),
                                   'SING_BOX_BIN': SB})


if __name__ == '__main__':
    unittest.main()
