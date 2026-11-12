"""Перенос состояния деплоя из checkout (infra/state, infra/.known_hosts) в VPN2_STATE_DIR."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
MGMT = '# тег → октет\nse 2\nnl 3\n'


class StateDirMigrateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.infra = self.root / 'checkout/infra'
        self.state = self.root / 'state'
        (self.infra / 'state').mkdir(parents=True)

    def migrate(self, before=''):
        script = f'source {ROOT}/infra/lib/state-dir.sh\ninstall -d -m 700 "$VPN2_STATE_DIR"\n{before}\nstate_dir_migrate "$1"\n'
        env = {**os.environ, 'VPN2_STATE_DIR': str(self.state), 'VPN2_DEPLOY_LOCK': str(self.root / 'lock')}
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', script, 'migrate', str(self.infra)],
                              env=env, capture_output=True, text=True, timeout=30)

    def old_layout(self):
        (self.infra / 'state/mgmt-ips.txt').write_text(MGMT)
        (self.infra / 'state/excluded-exits.txt').write_text('nl\n')
        (self.infra / 'state/rotate-nl-443.log').write_text('log\n')
        (self.infra / 'state/deploy.lock').touch()
        (self.infra / '.known_hosts').write_text('host key\n')
        (self.infra / '.known_hosts').chmod(0o644)
        (self.infra / '.known_hosts.old').write_text('old key\n')

    def assert_moved(self):
        self.assertEqual((self.state / 'mgmt-ips.txt').read_text(), MGMT)
        self.assertEqual((self.state / 'excluded-exits.txt').read_text(), 'nl\n')
        self.assertEqual((self.state / 'rotate-nl-443.log').read_text(), 'log\n')
        self.assertEqual((self.state / 'known_hosts').read_text(), 'host key\n')
        self.assertEqual((self.state / 'known_hosts.old').read_text(), 'old key\n')
        self.assertEqual((self.state / 'known_hosts').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.state / 'deploy.lock').exists(), 'замок не состояние')
        self.assertFalse((self.infra / 'state').exists(), 'infra/state остался в checkout')
        self.assertFalse((self.infra / '.known_hosts').exists())
        self.assertFalse((self.infra / '.known_hosts.old').exists())
        self.assertEqual([p.name for p in self.state.glob('*.tmp')], [])

    def test_moves_everything_and_repeats_cleanly(self):
        self.old_layout()
        for attempt in ('первый', 'повторный'):
            with self.subTest(attempt=attempt):
                result = self.migrate()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, '')
                self.assert_moved()
                self.assertFalse((self.state / 'checkout-old').exists())

    def test_no_old_layout_is_a_no_op(self):
        (self.infra / 'state').rmdir()
        result = self.migrate()
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertEqual(list(self.state.iterdir()), [])

    def test_interrupted_move_is_finished(self):
        # Обрыв после копии первого файла и до удаления источника; второй не начат.
        self.old_layout()
        self.state.mkdir(mode=0o700)
        (self.state / 'mgmt-ips.txt').write_text(MGMT)
        (self.state / 'excluded-exits.txt.tmp').write_text('обрыв')
        result = self.migrate()
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assert_moved()

    def test_failed_copy_keeps_source(self):
        self.old_layout()
        result = self.migrate(before='cp() { return 1; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.infra / 'state/mgmt-ips.txt').read_text(), MGMT)
        self.assertEqual((self.infra / '.known_hosts').read_text(), 'host key\n')
        self.assertFalse((self.state / 'mgmt-ips.txt').exists())

    def test_different_target_wins_and_source_is_kept(self):
        self.old_layout()
        self.state.mkdir(mode=0o700)
        (self.state / 'mgmt-ips.txt').write_text('se 5\n')
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / 'mgmt-ips.txt').read_text(), 'se 5\n')
        self.assertEqual((self.state / 'checkout-old/mgmt-ips.txt').read_text(), MGMT)
        self.assertIn('checkout-old/mgmt-ips.txt', result.stderr)
        self.assertFalse((self.infra / 'state').exists())


if __name__ == '__main__':
    unittest.main()
