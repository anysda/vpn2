"""Both partitions must run and either partition can fail the infra gate."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ParallelRunnerTests(unittest.TestCase):
    def test_each_partition_is_required(self):
        runner = Path(__file__).with_name('run_parallel.py')
        for failing in ('', 'pki', 'general'):
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                # A module each: two partitions, as the runner splits by module.
                for name in ('pki', 'general'):
                    (root / f'test_{name}.py').write_text(f'''
import unittest
class SampleTests(unittest.TestCase):
    def test_{name}(self):
        self.assertNotEqual({failing!r}, {name!r}, {name!r} + ' failure propagated')
''')
                result = subprocess.run([sys.executable, str(runner), '--dir', str(root)],
                                        text=True, capture_output=True, timeout=10, check=False)
                self.assertEqual(result.returncode, int(bool(failing)), result.stdout + result.stderr)
                self.assertEqual(result.stdout.count('Ran 1 test'), 2, result.stdout)
                if failing:
                    self.assertIn(f'{failing} failure propagated', result.stdout)


if __name__ == '__main__':
    unittest.main()
