import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from router_support import ROOT, SB, stage as router_stage
import watchdog_src


@unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
class ConfigPublishTests(unittest.TestCase):
    def test_invalid_generation_keeps_previous_config(self):
        for stage in ('router',):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                config = directory / ('config-base.json' if stage == 'router' else 'config.json')
                config.write_text('{"outbounds":[{"type":"direct","tag":"direct"}]}')
                before = config.read_bytes()
                result = self.publish(stage, directory, '{"invalid_field":true}')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(config.read_bytes(), before)
                self.assertFalse(list(directory.glob('.config*')))

    def test_valid_config_is_published_with_private_permissions(self):
        for stage in ('router',):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                body = '{"outbounds":[{"type":"direct","tag":"direct"}]}'
                result = self.publish(stage, directory, body)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                config = directory / ('config-base.json' if stage == 'router' else 'config.json')
                self.assertEqual(config.read_text().strip(), body)
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    @staticmethod
    def publish(stage, directory, body):
        if stage == 'router':
            # Базу роутера публикует шаг пакета; конфиг проверяет настоящий бинарник.
            def check(path):
                return subprocess.run([SB, 'check', '-c', str(path)], capture_output=True).returncode == 0
            try:
                router_stage.publish_config(body, directory / 'config-base.json', check)
            except router_stage.StageError as error:
                return subprocess.CompletedProcess([], 1, '', str(error))
            return subprocess.CompletedProcess([], 0, '', '')
        source = (ROOT / f'infra/scripts/{stage}.sh').read_text()
        source = 'TPL=' + source.split('TPL=', 1)[1].split('cat > /etc/systemd/system/', 1)[0]
        source = source.replace('/etc/sing-box', str(directory))
        source = source.replace('/tmp/anysda/sing-box-server.json.tpl', str(directory / 'template'))
        source = source.replace('/usr/local/bin/sing-box', '"$SING_BOX_BIN"')
        source = source.replace(watchdog_src.VENV_VPN2, str(watchdog_src.shim(directory)))
        (directory / 'template').write_text(body)
        shell = 'set -euo pipefail\nenvsubst() { command cat; }\n' + source
        return subprocess.run(['bash', '-c', shell], text=True, capture_output=True,
                              env={**os.environ, 'HOST_TAG': 'test', 'SING_BOX_BIN': SB})


if __name__ == '__main__':
    unittest.main()
