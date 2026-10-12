import tempfile
import unittest

from test_deploy import bash, source


class AptPauseTests(unittest.TestCase):
    def test_waits_for_either_apt_job(self):
        library = source('infra/lib/anysda-svc.sh')
        for busy_job in ('apt-daily.service', 'apt-daily-upgrade.service'):
            with self.subTest(busy_job=busy_job), tempfile.TemporaryDirectory() as directory:
                result = bash(library + f'\nBUSY_JOB={busy_job}\n' + '''
systemctl() {
  if [[ $1 == show ]]; then
    if [[ $3 == SubState ]]; then echo running
    elif [[ $4 == "$BUSY_JOB" && ! -f settled ]]; then echo activating
    else echo inactive; fi
  fi
}
sleep() { echo waited; touch settled; }
apt_wait_idle() { :; }
systemd-run() { :; }
apt_auto_pause
''', directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('waited', result.stdout)


if __name__ == '__main__':
    unittest.main()
