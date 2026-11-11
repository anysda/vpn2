"""restore передаёт стадиям режим деплоя (VPN2_DEVELOPMENT).

deploy.sh restore кладёт значение перед запуском anysda-restore.sh на entry,
а vpn2.backup.restore пишет его в env стадий (проверено в тестах пакета). Без этого стадия
30-frontend после деплоя --development идёт как установка по манифесту.
"""
import os
from pathlib import Path
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


class TestRestoreKeepsDeployMode(unittest.TestCase):
    def test_deploy_passes_mode_to_restore_command(self):
        text = (ROOT / 'infra/deploy.sh').read_text()
        lines = [line for line in text.splitlines() if 'anysda-restore.sh' in line and 'ssh_exec' in line]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('VPN2_DEVELOPMENT=${VPN2_DEVELOPMENT:-false} /usr/local/bin/anysda-restore.sh', lines[0])

if __name__ == '__main__':
    unittest.main()
