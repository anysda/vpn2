import configparser
import unittest

from test_deploy import source

ROLE = 'infra/ansible/roles/backup'


class BackupDisableTests(unittest.TestCase):
    def test_schedule_uses_entry_timezone_with_at_most_five_minutes_jitter(self):
        config = configparser.ConfigParser()
        config.read_string(source(f'{ROLE}/templates/anysda-backup.timer.j2'))
        self.assertEqual(config['Timer']['OnCalendar'], '*-*-* 02:00:00')
        self.assertEqual(config['Timer']['RandomizedDelaySec'], '300')
        # systemd по умолчанию добавляет ещё минуту AccuracySec поверх jitter.
        self.assertEqual(config['Timer'].get('AccuracySec', '1min'), '1us')
        self.assertEqual(config['Timer']['Persistent'], 'true')

    def test_disabled_stops_existing_timer(self):
        tasks = source(f'{ROLE}/tasks/main.yml')
        step = tasks.split('- name: Таймер выключенного бэкапа снят', 1)[1].split('\n- name:', 1)[0]
        self.assertIn('name: anysda-backup.timer', step)
        self.assertIn('enabled: false', step)
        self.assertIn('state: stopped', step)
        self.assertIn('when: not backup_enabled', step)


if __name__ == '__main__':
    unittest.main()
