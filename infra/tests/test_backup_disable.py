from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, source


class BackupDisableTests(unittest.TestCase):
    def test_disabled_stops_existing_timer(self):
        stage = source('infra/scripts/26-backup.sh')
        block = stage.split('if [[ "${BACKUP_ENABLED:-}" != "true" ]]; then', 1)[1]
        block = 'if [[ "${BACKUP_ENABLED:-}" != "true" ]]; then' + block.split('\nfi', 1)[0] + '\nfi\n'
        for enabled in ('false', ''):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                result = bash(f'BACKUP_ENABLED="{enabled}"\nHOST_TAG=ru\n'
                              'systemctl() { echo "systemctl $*" >> calls; }\n' + block, directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = Path(directory) / 'calls'
                self.assertTrue(calls.exists(), result.stdout)
                self.assertEqual(calls.read_text().splitlines(), ['systemctl disable --now anysda-backup.timer'])


if __name__ == '__main__':
    unittest.main()
