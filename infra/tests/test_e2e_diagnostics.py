"""A failed panel-e2e run keeps the browser server log next to the panel log."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))

# Docker without a daemon: records every call; the panel is ready at once
# and `logs <name>` of any other container prints a marker.
FAKE_DOCKER = '''#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
[ "$*" = 'logs diag' ] && echo 'panel init complete'
[ "$1" = logs ] && echo "log of $2"
cat > /dev/null
exit 0
'''


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class E2eDiagnosticsTests(unittest.TestCase):
    def test_failed_run_saves_browser_log_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            # The real scripts, with node_modules and the tools replaced by stubs.
            (tree / 'app').mkdir()
            for name in ('e2e', 'shared'):
                (tree / 'app' / name).symlink_to(ROOT / 'app' / name)
            (tree / 'infra').symlink_to(ROOT / 'infra')
            modules = tree / 'app/node_modules'
            write(modules / '@playwright/test/package.json', '{"version": "1.0.0"}')
            write(modules / '.bin/playwright', '#!/bin/sh\nexit 1\n')
            bin_dir = tree / 'bin'
            write(bin_dir / 'docker', FAKE_DOCKER)
            write(bin_dir / 'curl', '#!/bin/sh\necho ok\n')
            write(bin_dir / 'node', '#!/bin/sh\necho 1.0.0\n')
            report = tree / 'report'
            calls = tree / 'calls'
            result = subprocess.run(
                ['bash', str(tree / 'app/e2e/run-local.sh')],
                cwd=tree, text=True, capture_output=True, check=False, timeout=120,
                stdin=subprocess.DEVNULL,
                env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                     'DOCKER': str(bin_dir / 'docker'), 'CALLS': str(calls),
                     'E2E_SHARD': '1/1', 'E2E_SHARDS': '1', 'E2E_SKIP_BUILD': '1',
                     'E2E_CONTAINER': 'diag', 'E2E_REPORT_DIR': str(report)})
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual((report / 'browser.log').read_text(), 'log of diag-browser\n')
            history = calls.read_text().splitlines()
            saved = history.index('logs diag-browser')
            removed = history.index('rm -f diag-browser')
            self.assertLess(saved, removed)
            started = next(line for line in history if line.startswith('run -d --rm --init --name diag-browser'))
            self.assertIn('-e DEBUG=pw:server,pw:error', started)


if __name__ == '__main__':
    unittest.main()
