import tempfile
import unittest

from infra_support import bash, source


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

    def test_running_upgrade_is_stopped_after_current_package(self):
        """Свежая ОС: unattended-upgrade получает TERM, деплой не ждёт всё автообновление."""
        library = source('infra/lib/anysda-svc.sh')
        for pid, signalled in (('4242', True), ('4343', False)):
            with self.subTest(pid=pid), tempfile.TemporaryDirectory() as directory:
                result = bash(library + f'\necho {pid} > uu.pid\nAPT_UU_PID=uu.pid\n' + '''
systemctl() {
  if [[ $1 == show ]]; then
    if [[ $4 == apt-daily-upgrade.service && $3 == ActiveState && ! -f stopped ]]; then echo activating
    else echo inactive; fi
  fi
  return 0
}
# Процесс службы - только 4242; 4343 - pid из старого файла, теперь чужой.
grep() { if [[ ${!#} == /proc/* ]]; then [[ ${!#} == /proc/4242/cgroup ]]; else command grep "$@"; fi; }
kill() { echo "kill $*"; touch stopped; }
sleep() { touch stopped; }
apt_wait_idle() { :; }
systemd-run() { :; }
apt_auto_pause
''', directory)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual('kill -TERM 4242' in result.stdout, signalled, result.stdout)
                self.assertEqual('после текущего пакета' in result.stdout, signalled, result.stdout)

    def test_second_pause_does_not_mask_again(self):
        """Каждый mask - перечитывание systemd: повторная пауза уже замаскированного его не делает."""
        library = source('infra/lib/anysda-svc.sh')
        with tempfile.TemporaryDirectory() as directory:
            result = bash(library + '''
systemctl() {
  echo "$*" >> calls
  case $1 in
    is-enabled) cat "$2.state" 2>/dev/null || echo disabled ;;
    mask) shift 2; for u; do echo masked-runtime > "$u.state"; done ;;
    show) echo inactive ;;
  esac
  return 0
}
apt_wait_idle() { :; }
systemd-run() { :; }
apt_auto_pause
grep -c '^mask' calls > first
: > calls
apt_auto_pause
if grep '^mask' calls; then exit 1; fi
test "$(cat first)" -ge 1
''', directory)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
