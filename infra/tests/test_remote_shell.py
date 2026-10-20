"""Remote commands under nounset must not run the node's ~/.bashrc."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
# A nested bash started over ssh reads ~/.bashrc; Debian/Ubuntu's begins with
# [ -z "$PS1" ] && return, which fails under -u.
INVOCATION = re.compile(r'\bbash((?: +--?[a-z]+(?: +pipefail)?)+) +-c\b')


class RemoteShellTests(unittest.TestCase):
    def test_nounset_remote_bash_does_not_read_bashrc(self):
        found = []
        for path in sorted((ROOT / 'infra').rglob('*.sh')):
            if 'tests' in path.parts:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                for match in INVOCATION.finditer(line):
                    if re.search(r'(^| )-[a-z]*u|nounset', match[1]):
                        found.append((f'{path.relative_to(ROOT)}:{number}', match[1].strip()))
        self.assertTrue(found)
        with tempfile.TemporaryDirectory() as home:
            Path(home, '.bashrc').write_text('[ -z "$PS1" ] && return\n')
            env = {'HOME': home, 'PATH': '/usr/bin:/bin', 'SSH_CLIENT': '192.0.2.1 50000 22'}
            for place, options in found:
                with self.subTest(place):
                    result = subprocess.run(['bash', '-c', f'true; bash {options} -c "echo ok"'],
                                            env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                            text=True, timeout=30)
                    self.assertEqual((result.stdout, result.stderr), ('ok\n', ''))


if __name__ == '__main__':
    unittest.main()
