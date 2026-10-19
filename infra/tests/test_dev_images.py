"""deploy.sh --development runs the :dev images that images.yml published for the dev head.

VPN2_TEST_ROOT selects the unchanged baseline.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
WORKFLOW = ROOT / '.github/workflows/images.yml'
REVISION = '0123456789abcdef0123456789abcdef01234567'

FAKE_DOCKER = r'''#!/bin/sh
case "$1" in
  pull) echo "pull $2" >&2; [ "$FAKE_PULL" = ok ] ;;
  image)
    case "$4" in
      *revision*) echo "$FAKE_REVISION" ;;
      *) echo sha256:local-image-id ;;
    esac ;;
  *) exit 2 ;;
esac
'''


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_dev_push_publishes_branch_tag_for_every_image(self):
        self.assertRegex(self.text, r'push:\s*\n\s*branches: \[[^\]]*\bdev\b')
        names = re.findall(r'^\s*- name: (\w+)\n\s*context:', self.text, re.M)
        self.assertEqual(sorted(names), ['emergency', 'panel', 'tgbot'])
        self.assertIn('images: ghcr.io/${{ github.repository }}/${{ matrix.name }}', self.text)
        self.assertIn('type=ref,event=branch', self.text)
        self.assertIn('VPN2_REVISION=${{ github.sha }}', self.text)

    def test_runs_of_one_branch_never_overtake_each_other(self):
        # Without a queue an older run finishing later moves :dev back to its sha.
        block = re.search(r'^concurrency:\n((?:  .*\n)+)', self.text, re.M)
        self.assertIsNotNone(block, 'images.yml has no concurrency group')
        self.assertIn('group: images-${{ github.ref }}', block.group(1))
        self.assertIn('cancel-in-progress: false', block.group(1))


class DevelopmentImageTests(unittest.TestCase):
    def release_image(self, pull, pulled):
        with tempfile.TemporaryDirectory() as tmp:
            docker = Path(tmp) / 'docker'
            docker.write_text(FAKE_DOCKER)
            docker.chmod(0o755)
            env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', VPN2_DEVELOPMENT='true',
                       FAKE_PULL='ok' if pulled else 'fail', FAKE_REVISION=REVISION)
            script = f'source {ROOT / "infra/lib/release.sh"}; release_image panel "" {pull}'
            return subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, check=True)

    def test_pulled_image_names_its_revision(self):
        result = self.release_image('true', pulled=True)
        self.assertEqual(result.stdout, 'sha256:local-image-id\n')
        self.assertIn(f'panel: ghcr.io/anysda/vpn2/panel:dev revision {REVISION} (pulled)', result.stderr)

    def test_failed_pull_warns_and_names_stale_revision(self):
        result = self.release_image('true', pulled=False)
        self.assertEqual(result.stdout, 'sha256:local-image-id\n')
        self.assertIn('WARNING: [development] panel: pull ghcr.io/anysda/vpn2/panel:dev failed', result.stderr)
        self.assertIn(f'revision {REVISION} (local, pull failed)', result.stderr)

    def test_disabled_pull_names_local_revision(self):
        result = self.release_image('false', pulled=True)
        self.assertNotIn('pull ghcr.io', result.stderr)
        self.assertIn(f'revision {REVISION} (local, pull disabled)', result.stderr)


if __name__ == '__main__':
    unittest.main()
