"""A transient docker pull failure on a fresh Docker is retried, a final one is named."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
DIGEST = 'sha256:' + 'd' * 64
CONFIG = 'sha256:' + 'c' * 64
REFERENCE = 'example/tgbot@' + DIGEST
# What a freshly started dockerd printed when the layer extraction broke.
PULL_ERROR = ('failed to register layer: failed to cleanup "extract-1 sha256:dd57f9": '
              'snapshot does not exist: not found\n'
              'Error response from daemon: context canceled\n')

FAKE_DOCKER = r'''#!/bin/sh
state="$FAKE_DOCKER_STATE"
if [ "$1" = pull ]; then
  n=$(($(cat "$state/pulls" 2>/dev/null || echo 0) + 1))
  echo "$n" > "$state/pulls"
  if [ "$n" -le "$FAKE_DOCKER_FAILS" ]; then
    cat "$state/error" >&2
    exit 1
  fi
  touch "$state/pulled"
  exit 0
fi
if [ "$1" = image ] && [ "$2" = inspect ]; then
  if [ -e "$state/pulled" ]; then
    printf '[{"Id":"%s","Architecture":"%s","RepoDigests":["%s"]}]\n' \
      "$FAKE_DOCKER_ID" "$FAKE_DOCKER_ARCH" "$FAKE_DOCKER_REF"
    exit 0
  fi
  echo "Error: No such image: $3" >&2
  exit 1
fi
exit 1
'''


def release_module():
    path = ROOT / 'infra/lib/release.py'
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('release_pull_retry', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class PullRetryTests(unittest.TestCase):
    def setUp(self):
        self.release = release_module()
        self.arch = self.release.architecture()
        self.work = Path(tempfile.mkdtemp(prefix='vpn2-pull-retry-'))
        self.addCleanup(shutil.rmtree, self.work, True)
        tools = self.work / 'bin'
        tools.mkdir()
        (tools / 'docker').write_text(FAKE_DOCKER)
        (tools / 'docker').chmod(0o755)
        (self.work / 'error').write_text(PULL_ERROR)
        self.manifest = {'format': 1, 'commit': 'a' * 40, 'rollback': 'backward-compatible',
                         'images': {'tgbot': {self.arch: {'repository': 'example/tgbot',
                                                          'digest': DIGEST, 'config': CONFIG}}},
                         'binaries': {}}
        (self.work / 'release.json').write_text(json.dumps(self.manifest))
        self.environment = {'PATH': f'{tools}:' + os.environ['PATH'],
                            'FAKE_DOCKER_STATE': str(self.work), 'FAKE_DOCKER_ID': CONFIG,
                            'FAKE_DOCKER_ARCH': self.arch, 'FAKE_DOCKER_REF': REFERENCE}

    @staticmethod
    def pauses(sleep):
        # The patch is global: subprocess polls with sub-second sleeps of its own.
        return [c.args[0] for c in sleep.call_args_list if c.args and c.args[0] >= 1]

    def pulls(self):
        path = self.work / 'pulls'
        return int(path.read_text()) if path.exists() else 0

    def test_transient_pull_failure_is_retried(self):
        with patch.dict(os.environ, self.environment, FAKE_DOCKER_FAILS='1'), \
                patch('time.sleep') as sleep:
            handle = self.release.image(self.manifest, 'tgbot', arch=self.arch)
        print(f'one failed pull: {self.pulls()} pulls, pauses {self.pauses(sleep)}')
        self.assertEqual(handle, CONFIG)
        self.assertEqual(self.pulls(), 2, 'failed docker pull was not retried')
        self.assertEqual(self.pauses(sleep), [5])

    def test_final_pull_failure_names_image_and_docker_error(self):
        stderr = io.StringIO()
        argv = ['release.py', '--manifest', str(self.work / 'release.json'), 'image', 'tgbot']
        with patch.dict(os.environ, self.environment, FAKE_DOCKER_FAILS='99'), \
                patch('time.sleep') as sleep, patch.object(sys, 'argv', argv), \
                contextlib.redirect_stderr(stderr):
            code = self.release.main()
        message = stderr.getvalue()
        print(f'all pulls failed: code {code}, {self.pulls()} pulls, message: {message.strip()}')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.pulls(), 3, 'docker pull is not tried three times')
        self.assertEqual(self.pauses(sleep), [5, 15])
        self.assertIn('tgbot', message)
        self.assertIn(REFERENCE, message)
        self.assertIn('context canceled', message, 'docker error is hidden from the deploy output')


if __name__ == '__main__':
    unittest.main(verbosity=2)
