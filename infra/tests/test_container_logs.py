"""Production containers must bound their logs independently of daemon defaults."""
from pathlib import Path
import re
import shlex
import unittest


class ContainerLogsTest(unittest.TestCase):
    def test_production_containers_rotate_logs(self):
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        for stage in ('25-monitoring.sh', '30-frontend.sh', '35-telegram.sh'):
            with self.subTest(stage=stage):
                source = (scripts / stage).read_text()
                command = re.search(r'^docker run -d \\\n(?:.*\\\n)*.*', source, re.M)
                self.assertIsNotNone(command)
                args = shlex.split(command[0].replace('\\\n', ' '))
                self.assertIn('--log-driver', args)
                self.assertEqual(args[args.index('--log-driver') + 1], 'json-file')
                options = [args[i + 1] for i, arg in enumerate(args) if arg == '--log-opt']
                self.assertIn('max-size=10m', options)
                self.assertIn('max-file=3', options)


if __name__ == '__main__':
    unittest.main()
