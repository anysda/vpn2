"""Опечатки и неподдерживаемый YAML должны давать ошибку с путём ключа."""
import tempfile
from pathlib import Path
import unittest

import test_config_validation as validation

CONFIG = validation.CONFIG


class ConfigSchemaTests(unittest.TestCase):
    def test_bad_keys_types_and_structure(self):
        cases = [
            (CONFIG + 'sso:\n  enabeld: true\n', 'sso.enabeld'),
            (CONFIG + 'panle:\n  domain: vpn.example.com\n', 'panle'),
            (CONFIG + 'ports:\n  hy2_driect: 4443\n', 'ports.hy2_driect'),
            (CONFIG + 'sso:\n  enabled: tru\n', 'sso.enabled'),
            (CONFIG + 'sso:\n  password_login: flase\n', 'sso.password_login'),
            (CONFIG + 'backup:\n  enabled: flase\n', 'backup.enabled'),
            (CONFIG + 'backup:\n  s3: wrong\n', 'backup.s3'),
            (CONFIG + 'admin:\n  password: [one, two]\n', 'admin.password'),
            (CONFIG + 'admin:\n  password: null\n', 'admin.password'),
            (CONFIG + 'admin:\n  password: true\n', 'admin.password'),
            (CONFIG + 'admin:\n  password: "secret"junk\n', 'admin.password'),
            (CONFIG + 'admin:\n  password: "secret\\q"\n', 'admin.password'),
            (CONFIG + 'admin:\n  password: "unclosed\n', 'admin.password'),
            (CONFIG + 'admin:\n  password: |\n    secret\n', 'admin.password'),
            (CONFIG + 'admin:\n    password: secret\n', 'admin.password'),
            (CONFIG + 'entry:\n  host: 192.0.2.4\n', 'entry'),
            (CONFIG + 'panel:\n  domain: one.test\n  domain: two.test\n', 'panel.domain'),
            (CONFIG + 'panel: wrong\n', 'panel'),
            (CONFIG + 'entry_resources:\n  vm_memroy_mib: 512\n', 'entry_resources.vm_memroy_mib'),
            (CONFIG + 'entry_resources:\n  adaway_enabled: yes\n', 'entry_resources.adaway_enabled'),
            (CONFIG + 'entry_resources:\n  retention_days: 0\n', 'entry_resources.retention_days'),
            (CONFIG + 'entry_resources:\n  querylog_size: 12x\n', 'entry_resources.querylog_size'),
        ]
        for config, key in cases:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                result = validation.ConfigValidationTests.convert(self, config, directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn('secret', result.stderr)

    def test_entry_resources_accepted(self):
        config = CONFIG + ('entry_resources:\n  querylog_size: 0\n  adaway_enabled: false\n'
                           '  retention_days: "11"\n  vm_memory_mib: 384\n')
        with tempfile.TemporaryDirectory() as directory:
            result = validation.ConfigValidationTests.convert(self, config, directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_bandwidth_and_window_scale_accepted(self):
        config = (CONFIG + '    hy2_up_mbps: 300\n    hy2_down_mbps: 900\n'
                  'youtube:\n  route: zapret\n  window_scale: 1\n')
        with tempfile.TemporaryDirectory() as directory:
            result = validation.ConfigValidationTests.convert(self, config, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = ''.join(p.read_text() for p in (Path(directory) / 'infra/envs').glob('*.env'))
            self.assertIn('YT_ZAPRET_WINDOW_SCALE=', env)
            self.assertIn('SE_HY2_UP_MBPS=', env)


if __name__ == '__main__':
    unittest.main()
