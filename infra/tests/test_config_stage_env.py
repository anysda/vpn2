"""Многострочный SSH-пароль не должен попадать в env удалённой стадии."""
from pathlib import Path
import tempfile
import unittest

import test_config_validation as validation
from test_deploy import bash, function


class StageEnvTests(unittest.TestCase):
    def test_multiline_password_is_removed_as_one_assignment(self):
        config = validation.CONFIG.replace("'entry secret'", '"FIRST_SECRET\\nSECOND_SECRET"')
        with tempfile.TemporaryDirectory() as directory:
            result = validation.ConfigValidationTests.convert(self, config, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            root = Path(directory)
            (root / 'infra/scripts').mkdir()
            (root / 'infra/scripts/30-frontend.sh').touch()
            result = bash(function('run_stage_on_host') + '''
DEPLOY_ROOT="$PWD/infra"
load_env() { :; }
log() { :; }
die() { return 1; }
push() { return 1; }
run_stage_on_host 30-frontend ru || :
bash -n "$DEPLOY_ROOT/secrets/rendered/ru.env"
''', directory)
            rendered = (root / 'infra/secrets/rendered/ru.env').read_text()
            self.assertNotIn('SECOND_SECRET', rendered)
            self.assertNotIn('FIRST_SECRET', rendered)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
