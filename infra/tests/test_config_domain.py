import subprocess
import tempfile
import unittest

import test_config_validation as validation
from test_setup_input import prepare, run_setup


class DomainTests(unittest.TestCase):
    def test_domain_rejects_config_injection(self):
        for domain in ('vpn.example.com\\n:80 { respond owned }', 'two names.test', '*.example.com',
                       'https://example.com', '-bad.example', 'bad..example', '"bad".example'):
            with self.subTest(domain=domain), tempfile.TemporaryDirectory() as directory:
                config = validation.CONFIG + 'panel:\n  domain: "' + domain.replace('"', '\\"') + '"\n'
                result = validation.ConfigValidationTests.convert(self, config, directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('panel.domain', result.stderr)

    def test_domain_normalization_in_env(self):
        for domain, expected in (('VPN.Example.COM', 'vpn.example.com'),
                                 ('ПРИМЕР.РФ', 'xn--e1afmkfd.xn--p1ai')):
            with self.subTest(domain=domain), tempfile.TemporaryDirectory() as directory:
                result = validation.ConfigValidationTests.convert(
                    self, validation.CONFIG + f'panel:\n  domain: {domain}\n', directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run(['bash', '-euc', 'source infra/envs/ru.env; printf %s "$PANEL_DOMAIN"'],
                                        cwd=directory, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.stdout, expected)

    def test_wizard_accepts_idn(self):
        answers = ['1', 'root secret', '192.0.2.2', 'exit secret', 'se', '', '', '',
                   'y', 'ПРИМЕР.РФ', 'n', 'n', 'n', 'y']
        with tempfile.TemporaryDirectory() as directory:
            root = prepare(directory)
            code, output = run_setup(directory, '\n'.join(answers) + '\n')
            self.assertEqual(code, 0, output)
            self.assertIn('xn--e1afmkfd.xn--p1ai', (root / 'config.yaml').read_text())


if __name__ == '__main__':
    unittest.main()
