"""Fixed test listeners must not compete with Linux ephemeral client ports."""

import os
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


class E2ePortTests(unittest.TestCase):
    def test_fixed_listeners_and_their_clients_use_low_ports(self):
        paths = [ROOT / 'app/playwright.config.ts',
                 *(ROOT / 'app/e2e').rglob('*.sh'),
                 *(ROOT / 'app/e2e').rglob('*.mjs'),
                 *(ROOT / 'app/e2e').rglob('*.ts'),
                 *(ROOT / '.github/workflows').glob('*.yml')]
        # Match HTTP listeners/clients, not VPN endpoint fixtures or snapshots.
        patterns = [r'https?://(?:127\.0\.0\.1|localhost|panel):(\d+)',
                    r'\bPORT[=:]\s*[\'"]?(\d+)', r'\bPORT=\$\{[^}]*:-(\d+)\}',
                    r'--port[ =](\d+)', r'\.listen\((\d+)',
                    r'reverse_proxy panel:(\d+)', r'-p\s+(?:127\.0\.0\.1:)?(\d+):']
        checked = 0
        for path in paths:
            for pattern in patterns:
                for match in re.finditer(pattern, path.read_text()):
                    checked += 1
                    with self.subTest(path=str(path.relative_to(ROOT)), value=match.group()):
                        self.assertLess(int(match[1]), 32768)
        self.assertGreater(checked, 20)

    def test_default_panel_url_matches_shell_and_shard_runners(self):
        shell = (ROOT / 'app/e2e/run-local.sh').read_text()
        shard = (ROOT / 'app/e2e/run-shards.py').read_text()
        port = re.search(r'PORT=\$\{E2E_PORT:-(\d+)\}', shell)[1]
        self.assertIn(f'os.environ.get("E2E_PORT", "{port}")', shard)
        for file in ['app/playwright.config.ts', 'app/e2e/support/fixtures.ts']:
            self.assertIn(f"'http://127.0.0.1:{port}'", (ROOT / file).read_text())

    def test_image_healthcheck_uses_the_configured_listener(self):
        dockerfile = (ROOT / 'app/Dockerfile').read_text()
        healthcheck = dockerfile.split('HEALTHCHECK', 1)[1].split('\n\n', 1)[0]
        command = healthcheck.split('CMD ', 1)[1]
        for port in ('31821', '51821'):
            result = subprocess.check_output(
                ['sh', '-c', 'curl() { printf "%s\\n" "$@"; }; ' + command],
                env={**os.environ, 'PORT': port}, text=True,
            )
            self.assertIn(f'http://127.0.0.1:{port}/api/version', result.splitlines())


if __name__ == '__main__':
    unittest.main()
