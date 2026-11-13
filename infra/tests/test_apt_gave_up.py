import tempfile
import unittest

from infra_support import bash, source


class AptGaveUpTests(unittest.TestCase):
    """Исчерпанное ожидание apt не повторяется вторым кругом в том же деплое."""

    def run_library(self, tail):
        library = source('infra/lib/anysda-svc.sh')
        with tempfile.TemporaryDirectory() as directory:
            result = bash('APT_GAVE_UP=$PWD/gave-up\n' + library + '''
APT_LOCK_TIMEOUT=0
# Часы библиотеки - SECONDS. После unset это обычная переменная: время идёт
# только от подменённого sleep, настоящие часы на вывод не влияют.
unset SECONDS
SECONDS=100
sleep() { echo "sleep $*" >>sleeps; SECONDS=$(( SECONDS + $1 )); }
apt-get() { echo "$*" >>calls; }
''' + tail, directory)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_second_wait_returns_at_once_and_apt_gets_short_timeout(self):
        out = self.run_library('''
apt_lock_holders() { echo 1; }
apt_wait_idle
test -e gave-up
APT_LOCK_TIMEOUT=1200
apt_get install -y x
test ! -e sleeps
cat calls
''')
        self.assertIn('apt так и не освободился за 0 с, продолжаю', out)
        self.assertIn('apt всё ещё занят, ожидание уже исчерпано, продолжаю', out)
        self.assertIn('-o DPkg::Lock::Timeout=60 install -y x', out)

    def test_message_does_not_depend_on_wall_clock(self):
        # Секунда на настоящих часах перевалила посреди ожидания: число в
        # строке считается по часам библиотеки, а они в тесте подставные.
        out = self.run_library('''
apt_lock_holders() { command sleep 1.1; echo 1; }
apt_wait_idle
''')
        self.assertIn('apt так и не освободился за 0 с, продолжаю', out)

    def test_wait_lasts_exactly_the_timeout(self):
        out = self.run_library('''
APT_LOCK_TIMEOUT=10
apt_lock_holders() { echo 1; }
apt_wait_idle
cat sleeps
''')
        self.assertIn('apt так и не освободился за 10 с, продолжаю', out)
        self.assertEqual(out.count('sleep 5'), 2, out)

    def test_idle_apt_clears_the_mark(self):
        out = self.run_library('''
apt_lock_holders() { echo 1; }
apt_wait_idle
apt_lock_holders() { return 1; }
APT_LOCK_TIMEOUT=1200
apt_get update
test ! -e gave-up
cat calls
''')
        self.assertIn('-o DPkg::Lock::Timeout=1200 update', out)


if __name__ == '__main__':
    unittest.main()
