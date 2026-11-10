"""restore после установки по манифесту не зависит от /tmp/anysda.

Манифест установленного выпуска лежит в состоянии деплоя: туда его кладёт
deploy.sh (и при --manifest), оттуда его берут стадии anysda-restore.sh.
Манифест из архива текущий не заменяет.
"""
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

from test_restore_keeps_deploy_mode import ROOT, stage_env_block


def bash(script):
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, timeout=30)


class RestoreReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()

    def stage_env(self):
        envs = self.root / 'infra/envs'
        envs.mkdir(parents=True, exist_ok=True)
        (envs / 'all.env').write_text('AGH_PORT=3000\n')
        (envs / 'ru.env').write_text('HOST=ru\n')
        work = self.root / 'work'
        work.mkdir(exist_ok=True)
        result = bash(f'SOURCE_ROOT={self.root}\nWORK={work}\nVPN2_STATE_DIR={self.state}\n{stage_env_block()}\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        return (work / 'ru.env').read_text().splitlines()

    def test_stages_read_manifest_from_deploy_state(self):
        (self.state / 'release-manifest.json').write_text('{}\n')
        self.assertIn(f'VPN2_RELEASE_MANIFEST={self.state}/release-manifest.json', self.stage_env())

    def test_without_stored_manifest_stages_keep_default(self):
        self.assertFalse([line for line in self.stage_env() if line.startswith('VPN2_RELEASE_MANIFEST=')])

    def test_stage_default_path_is_the_one_restore_overrides(self):
        text = (ROOT / 'infra/lib/release.sh').read_text()
        self.assertEqual(text.count('"${VPN2_RELEASE_MANIFEST:-/tmp/anysda/release-manifest.json}"'), 2)

    def release_start(self, manifest):
        library = ROOT / 'infra/lib/deploy-release.sh'
        return bash(f'''
source {shlex.quote(str(library))}
DEPLOY_ROOT={shlex.quote(str(ROOT / 'infra'))}
VPN2_STATE_DIR={self.state}
VPN2_RELEASE_MANIFEST={manifest}
python3() {{ :; }}
install_prereqs() {{ exit 0; }}
release_start deploy
''')

    def test_deploy_keeps_explicit_manifest_in_state(self):
        manifest = self.root / 'given.json'
        manifest.write_text('{"commit": "given"}\n')
        result = self.release_start(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / 'release-manifest.json').read_text(), manifest.read_text())

    def test_deploy_accepts_manifest_already_in_state(self):
        manifest = self.state / 'release-manifest.json'
        manifest.write_text('{"commit": "stored"}\n')
        result = self.release_start(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(manifest.read_text(), '{"commit": "stored"}\n')

    def restore_state(self):
        text = (ROOT / 'infra/lib/anysda-restore.sh').read_text()
        match = re.search(r'^STEP="восстановление состояния деплоя"\n.*?^fi$', text, re.S | re.M)
        self.assertTrue(match, 'блок восстановления состояния деплоя не найден')
        block = match.group(0).replace('/var/lib/anysda-vpn2/state', str(self.state)).replace('-o root -g root ', '')
        bundle = self.root / 'bundle/deploy-state'
        bundle.mkdir(parents=True)
        (bundle / 'release-manifest.json').write_text('{"commit": "archived"}\n')
        (bundle / 'known_hosts').write_text('archived\n')
        work = self.root / 'work'
        work.mkdir(exist_ok=True)
        result = bash(f'''
BUNDLE_DIR={self.root}/bundle
WORK={work}
TOUCHED=()
copy_into() {{ cp -a "$1/." "$2/"; }}
{block}
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_archived_manifest_does_not_replace_installed_one(self):
        (self.state / 'release-manifest.json').write_text('{"commit": "installed"}\n')
        self.restore_state()
        self.assertEqual((self.state / 'release-manifest.json').read_text(), '{"commit": "installed"}\n')
        self.assertEqual((self.state / 'known_hosts').read_text(), 'archived\n')

    def test_archived_manifest_fills_missing_one(self):
        self.restore_state()
        self.assertEqual((self.state / 'release-manifest.json').read_text(), '{"commit": "archived"}\n')


if __name__ == '__main__':
    unittest.main()
