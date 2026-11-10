import tempfile
import unittest

from test_deploy import bash, source


class AptGaveUpTests(unittest.TestCase):
    """Исчерпанное ожидание apt не повторяется вторым кругом в том же деплое."""

    def run_library(self, tail):
        library = source('infra/lib/anysda-svc.sh')
        with tempfile.TemporaryDirectory() as directory:
            result = bash('APT_GAVE_UP=$PWD/gave-up\n' + library + '''
APT_LOCK_TIMEOUT=0
sleep() { echo "sleep $*" >>sleeps; }
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
