"""Both partitions must run and either partition can fail the infra gate."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ParallelRunnerTests(unittest.TestCase):
    def test_each_partition_is_required(self):
        source = Path(__file__).with_name('run_parallel.py').read_text()
        for failing in ('', 'pki', 'general'):
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'run_parallel.py').write_text(source)
                (root / 'test_sample.py').write_text(f'''
import unittest
class PkiTests(unittest.TestCase):
    def test_pki(self):
        self.assertNotEqual({failing!r}, 'pki', 'pki failure propagated')
class GeneralTests(unittest.TestCase):
    def test_general(self):
        self.assertNotEqual({failing!r}, 'general', 'general failure propagated')
''')
                result = subprocess.run([sys.executable, str(root / 'run_parallel.py')],
                                        text=True, capture_output=True, timeout=10, check=False)
                self.assertEqual(result.returncode, int(bool(failing)), result.stdout + result.stderr)
                self.assertEqual(result.stdout.count('Ran 1 test'), 2, result.stdout)
                if failing:
                    self.assertIn(f'{failing} failure propagated', result.stdout)


if __name__ == '__main__':
    unittest.main()
