"""restore после установки по манифесту не зависит от /tmp/anysda.

Манифест установленного выпуска лежит в состоянии деплоя: туда его кладёт
deploy.sh (и при --manifest), оттуда его берут стадии restore. Сам restore живёт в пакете:
окружение стадий и судьба манифеста из архива проверены в тестах vpn2.backup (stages, apply).
"""
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from test_restore_keeps_deploy_mode import ROOT


def bash(script):
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, timeout=30)


class RestoreReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()

    def test_stage_default_path_is_the_one_restore_overrides(self):
        text = (ROOT / 'infra/ansible/roles/emergency/files/release.sh').read_text()
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

if __name__ == '__main__':
    unittest.main()
