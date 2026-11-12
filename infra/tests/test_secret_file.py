from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, function


class SecretFileTests(unittest.TestCase):
    def test_partial_secret_collection_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = bash(function('pull_secrets') + '''
umask 022
DEPLOY_ROOT=$PWD TMP=$PWD
log() { :; }
ok() { :; }
warn() { :; }
die() { echo "$*" >&2; exit 1; }
exit_tags() { echo se; }
expand_hosts() { echo se; }
service() { printf 'DIRECT=ABCDEFGHIJKLMNOP\\n' > "$DEPLOY_ROOT/secrets/foreign-secrets.env"; return 1; }
pull_secrets
''', directory)
            self.assertNotEqual(result.returncode, 0)
            secret = root / 'secrets/foreign-secrets.env'
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
