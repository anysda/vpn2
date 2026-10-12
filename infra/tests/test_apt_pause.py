import tempfile
import unittest

from test_deploy import bash, source


class AptPauseTests(unittest.TestCase):
    def test_mask_does_not_fail_running_timers_and_clears_old_failure(self):
        library = source('infra/lib/anysda-svc.sh')
        for initial in ('active', 'failed'):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as directory:
                result = bash(library + f'\nINITIAL={initial}\n' + '''
for timer in apt-daily.timer apt-daily-upgrade.timer; do
  echo "$INITIAL" > "$timer"
done
systemctl() {
  local action=$1 unit
  shift
  if [[ $action == show ]]; then echo inactive; return; fi
  for unit in "$@"; do
    [[ $unit == apt-daily.timer || $unit == apt-daily-upgrade.timer ]] || continue
    case "$action" in
      stop)
        [[ $(cat "$unit") != active ]] || echo inactive > "$unit" ;;
      mask)
        if [[ $(cat "$unit") == active ]]; then
          echo failed > "$unit"
          touch trigger-vanished
        fi ;;
      reset-failed)
        [[ $(cat "$unit") != failed ]] || echo inactive > "$unit" ;;
    esac
  done
  return 0
}
apt_wait_idle() { :; }
systemd-run() { :; }
apt_auto_pause
test ! -e trigger-vanished
for timer in apt-daily.timer apt-daily-upgrade.timer; do
  test "$(cat "$timer")" = inactive
done
''', directory)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
