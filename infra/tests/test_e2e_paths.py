"""The selector used by CI and the optional local gate, including Git events."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
SCRIPT = ROOT / 'infra/tests/e2e_paths.py'
sys.path.insert(0, str(SCRIPT.parent))
import e2e_paths  # noqa: E402

# sys.path of the stand scripts and of the code they run: a module name resolves against these.
IMPORT_ROOTS = ('infra/tests', 'infra/vpn2/src', 'telegram')
REFERENCE = re.compile(r'(?<![\w.-])((?:infra|telegram)/[\w.-]+(?:/[\w.-]+)*)')
IMPORT = re.compile(r'^\s*(?:from|import)\s+(\w+)|import_module\(\'(\w+)\'\)', re.M)


def e2e_inputs():
    """Files that app/e2e names (copies, runs, reads) or imports; imports are followed through the tests."""
    tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True,
                             text=True).stdout.rstrip('\0').split('\0')
    under = lambda top: {f for f in tracked if f == top or f.startswith(top.rstrip('/') + '/')}
    scan, found = sorted(under('app/e2e')), set()
    while scan:
        path = scan.pop()
        text = (ROOT / path).read_text(errors='replace')
        new = set()
        for ref in REFERENCE.findall(text) if path.startswith('app/') else ():
            if ref not in IMPORT_ROOTS:
                new |= under(ref)
        for name in filter(None, (a or b for a, b in IMPORT.findall(text))):
            for root in IMPORT_ROOTS:
                new |= under(f'{root}/{name}.py') | under(f'{root}/{name}')
        new -= found
        found |= new
        scan += [f for f in new if f.endswith('.py') and f.startswith(('infra/tests/', 'telegram/tests/'))]
    return found


class E2ePathsTests(unittest.TestCase):
    def test_every_input_of_the_stand_selects_e2e(self):
        inputs = e2e_inputs()
        for path in ['infra/vpn2/src/vpn2/control/backend.py', 'infra/vpn2/src/vpn2/manager.py',
                     'infra/vpn2/src/vpn2/config.py', 'infra/ansible/roles/ikev2/files/anysda-ikev2-sync.py',
                     'infra/tests/router_support.py', 'telegram/tests/test_qr_size.py']:
            self.assertIn(path, inputs)
        self.assertEqual(sorted(p for p in inputs if not e2e_paths.needs_e2e([p])), [])

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
                (['infra/scripts/21-failover-watchdog.sh'], 'false'),
                (['telegram/bot.py', 'README.md'], 'false'),
                (['app/app/pages/index.vue'], 'true'),
                (['infra/deploy.sh', 'app/e2e/run-local.sh'], 'true'),
                (['app/shared/vpn-pools.json'], 'true'),
                (['infra/vpn2/src/vpn2/resources.py'], 'true'),
                (['infra/vpn2/src/vpn2/pki.py'], 'true'),
                (['infra/vpn2/src/vpn2/containers/panel.py'], 'true'),
                (['infra/tests/speedtest.py'], 'true'),
                (['infra/vpn2/src/vpn2/watchdog/hy2_bandwidth.py'], 'true'),
                (['infra/tests/test_speedtest.py'], 'true'),
                (['infra/tests/test_panel_proxy.py'], 'true'),
                (['infra/ansible/roles/router/templates/config-base.json.j2'], 'true'),
                (['infra/tests/router_support.py'], 'true'),
                (['infra/tests/e2e_paths.py'], 'true'),
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
                    if expected == 'false':
                        git('update-ref', 'refs/remotes/origin/dev', base)
                        local = subprocess.run(
                            ['bash', str(ROOT / 'app/e2e/run-local.sh'), '--changed-only'],
                            cwd=repo, text=True, capture_output=True, check=False,
                            env={**os.environ, 'DOCKER': 'false', 'E2E_SKIP_BUILD': '1'})
                        self.assertEqual(local.returncode, 0, local.stdout + local.stderr)
                        self.assertIn('panel-e2e: skipped', local.stdout)
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
