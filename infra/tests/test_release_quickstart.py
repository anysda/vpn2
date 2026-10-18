"""Missing release metadata must give an actionable, fail-closed install path."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from test_release import ROOT


class ReleaseQuickstartTests(unittest.TestCase):
    def test_missing_manifest_explains_download_before_remote_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'release-manifest.json'
            script = f'''
source {shlex.quote(str(ROOT / 'infra/lib/deploy-release.sh'))}
install_prereqs() {{ echo UNEXPECTED_REMOTE_CHANGE; exit 99; }}
release_start
'''
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                                    env=dict(os.environ, DEPLOY_ROOT=str(ROOT / 'infra'),
                                             VPN2_RELEASE_MANIFEST=str(missing), VPN2_DEVELOPMENT='false'),
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('UNEXPECTED_REMOTE_CHANGE', result.stdout)
            self.assertIn('release-<commit>', result.stderr)
            self.assertIn('GitHub login required', result.stderr)
            self.assertIn('./deploy.sh --manifest', result.stderr)
            self.assertIn('For development only: ./deploy.sh --development', result.stderr)
            self.assertNotIn('FileNotFoundError', result.stderr)
            print(result.stderr.strip())

    def test_quickstart_pins_checkout_and_manifest_to_successful_build(self):
        quickstart = (ROOT / 'README.md').read_text().split('**2.', 1)[1].split('**5.', 1)[0]
        for command in ('gh auth login', '--workflow images.yml --branch dev', '--status success',
                        '--name "release-$commit"', 'git checkout --detach "$commit"',
                        'release.py --manifest release-manifest.json check .',
                        './deploy.sh --manifest release-manifest.json'):
            self.assertIn(command, quickstart)
        self.assertNotIn('\n./deploy.sh\n', quickstart)


if __name__ == '__main__':
    unittest.main(verbosity=2)
