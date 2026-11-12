"""deploy.sh --development runs the :dev images that images.yml published for the dev head.

VPN2_TEST_ROOT selects the unchanged baseline.
"""
import os
from pathlib import Path
import re
import unittest

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
WORKFLOW = ROOT / '.github/workflows/images.yml'


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


if __name__ == '__main__':
    unittest.main()
