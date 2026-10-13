"""The selector used by CI and the optional local gate, including Git events."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
SCRIPT = ROOT / 'infra/lib/e2e_paths.py'


class E2ePathsTests(unittest.TestCase):
    def test_paths_and_events(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def git(*args):
                return subprocess.check_output(['git', *args], cwd=repo, text=True).strip()

            git('init', '-q')
            git('config', 'user.email', 'test@example.test')
            git('config', 'user.name', 'test')
            git('commit', '--allow-empty', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            for paths, expected in [
                (['infra/scripts/20-ru-router.sh'], 'false'),
                (['telegram/bot.py', 'README.md'], 'false'),
                (['app/app/pages/index.vue'], 'true'),
                (['infra/deploy.sh', 'app/e2e/run-local.sh'], 'true'),
                (['app/shared/vpn-pools.json'], 'true'),
                (['infra/lib/entry_resources.py'], 'true'),
                (['infra/scripts/29-openvpn.sh'], 'true'),
                (['infra/scripts/30-frontend.sh'], 'true'),
                (['infra/tools/speedtest.py'], 'true'),
                (['infra/lib/config2env.py'], 'true'),
                (['infra/lib/hy2_bandwidth.py'], 'true'),
                (['infra/tests/test_speedtest.py'], 'true'),
                (['infra/tests/test_deploy.py'], 'true'),
                (['infra/tests/test_panel_proxy.py'], 'true'),
                (['infra/lib/e2e_paths.py'], 'true'),
                (['.github/workflows/checks.yml'], 'true'),
                (['app/a\nfile.ts'], 'true'),
            ]:
                with self.subTest(paths=paths):
                    for path in paths:
                        file = repo / path
                        file.parent.mkdir(parents=True, exist_ok=True)
                        file.touch()
                    git('add', '.')
                    git('commit', '-qm', 'change')
                    head = git('rev-parse', 'HEAD')
                    actual = subprocess.check_output(
                        ['python3', str(SCRIPT), '--base', base], cwd=repo, text=True).strip()
                    self.assertEqual(actual, expected)
                    for name, event in [
                        ('push', {'before': base, 'after': head}),
                        ('pull_request', {'pull_request': {'base': {'sha': base}, 'head': {'sha': head}}}),
                    ]:
                        event_file = repo / '.git/event.json'
                        event_file.write_text(json.dumps(event))
                        actual = subprocess.check_output(
                            ['python3', str(SCRIPT), '--github'], cwd=repo, text=True,
                            env={**os.environ, 'GITHUB_EVENT_PATH': str(event_file),
                                 'GITHUB_EVENT_NAME': name}).strip()
                        self.assertEqual(actual, expected)
                    # Deletion has the same dependency footprint.
                    for path in paths:
                        (repo / path).unlink()
                    git('add', '.')
                    git('commit', '-qm', 'delete')
                    actual = subprocess.check_output(
                        ['python3', str(SCRIPT), '--base', head],
                        cwd=repo, text=True).strip()
                    self.assertEqual(actual, expected)
                    base = git('rev-parse', 'HEAD')
            for base in ['0' * 40, 'missing-ref']:
                actual = subprocess.check_output(
                    ['python3', str(SCRIPT), '--base', base], cwd=repo, text=True).strip()
                self.assertEqual(actual, 'true')


if __name__ == '__main__':
    unittest.main()
