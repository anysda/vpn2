from pathlib import Path
import configparser
import tempfile
import unittest

from test_deploy import bash, source


class BackupDisableTests(unittest.TestCase):
    def test_schedule_uses_entry_timezone_with_at_most_five_minutes_jitter(self):
        stage = source('infra/scripts/26-backup.sh')
        timer = stage.split("cat > /etc/systemd/system/anysda-backup.timer <<'EOF'\n", 1)[1].split('\nEOF', 1)[0]
        config = configparser.ConfigParser()
        config.read_string(timer)
        self.assertEqual(config['Timer']['OnCalendar'], '*-*-* 02:00:00')
        self.assertEqual(config['Timer']['RandomizedDelaySec'], '300')
        # systemd по умолчанию добавляет ещё минуту AccuracySec поверх jitter.
        self.assertEqual(config['Timer'].get('AccuracySec', '1min'), '1us')
        self.assertEqual(config['Timer']['Persistent'], 'true')

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
