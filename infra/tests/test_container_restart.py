"""Critical daemons must be restored when the Docker daemon restarts."""
from pathlib import Path
import re
import unittest


class ContainerRestartTest(unittest.TestCase):
    def test_daemon_restart_restores_all_services(self):
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        for stage in ('25-monitoring.sh', '30-frontend.sh', '35-telegram.sh'):
            with self.subTest(stage=stage):
                policy = re.search(r'--restart\s+(\S+)', (scripts / stage).read_text())
                self.assertIsNotNone(policy)
                self.assertEqual(policy[1], 'always')


if __name__ == '__main__':
    unittest.main()
