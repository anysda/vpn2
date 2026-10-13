import subprocess
import tempfile
import unittest

import test_config_validation as validation
from test_deploy import source
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

    def test_client_configs_use_panel_domain_unless_public_host_is_set(self):
        # Настоящие подстановки docker run панели из 30-frontend, без Docker.
        frontend = ' '.join(line.strip().removesuffix('\\') for line in
                            source('infra/scripts/30-frontend.sh').splitlines()
                            if '-e NUXT_WG_PUBLIC_HOST=' in line or '-e NUXT_OVPN_PUBLIC_HOST=' in line)
        script = 'source infra/envs/all.env; source infra/envs/ru.env; printf "%s|" ' + frontend
        cases = (('', '192.0.2.1'), ('panel:\n  domain: VPN.Example.COM\n', 'vpn.example.com'))
        for extra, expected in cases:
            for public in ('', 'edge.example.net'):
                config = validation.CONFIG + extra
                if public:
                    config = config.replace("password: 'entry secret'",
                                            f"password: 'entry secret'\n  public_host: {public}", 1)
                with self.subTest(extra=extra, public=public), tempfile.TemporaryDirectory() as directory:
                    result = validation.ConfigValidationTests.convert(self, config, directory)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = subprocess.run(['bash', '-euc', script], cwd=directory,
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    host = public or expected
                    self.assertEqual(result.stdout,
                                     f'-e|NUXT_WG_PUBLIC_HOST={host}|-e|NUXT_OVPN_PUBLIC_HOST={host}|')

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
