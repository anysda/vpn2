from pathlib import Path
import tempfile
import unittest

from test_deploy import write_envs


class SecretLogTests(unittest.TestCase):
    def test_yaml_error_hides_value(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'config.yaml').write_text("entry:\n  password: 'CANARY_UNCLOSED_SECRET\n")
            result = write_envs(directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('ERROR: config.yaml:', result.stderr)
            self.assertNotIn('CANARY_UNCLOSED_SECRET', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
