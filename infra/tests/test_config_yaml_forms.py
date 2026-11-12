"""config.yaml в той форме, в какой его пишет yaml.safe_dump."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_deploy import ROOT, values, write_envs
from vpn2.setup import merge_setup_config, parse_yaml


# Дословный вывод yaml.safe_dump: ключи по алфавиту, список без отступа,
# длинная строка перенесена на 80 символах.
SAFE_DUMP = """admin:
  password: adm pw
  ssh_pubkey: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBkCV2mrwNtd8HZsr2fO8QTtR3dQ5Q2sL8kQ5Wx9h4yZ
    user@host
  user: admin
backup:
  enabled: true
  local:
    dir: /var/backups/anysda
  passphrase: backup pass
entry:
  host: 192.0.2.1
  password: entry pass
exits:
- host: 192.0.2.2
  password: exit pass
  tag: se
- host: 192.0.2.3
  hy2_direct_port: 8443
  password: p2
  tag: nl
"""

PUBKEY = ('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBkCV2mrwNtd8HZsr2fO8QTtR3dQ5Q2sL8kQ5Wx9h4yZ'
          ' user@host')


class SafeDumpConfigTests(unittest.TestCase):
    def convert(self, config, directory):
        (Path(directory) / 'config.yaml').write_text(config)
        return write_envs(directory, env={'HOME': directory})

    def envs(self, directory):
        return {node: values(directory, node) for node in ('ru', 'se', 'nl')}

    def test_safe_dump_output_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.convert(SAFE_DUMP, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            envs = self.envs(directory)
            self.assertEqual(envs['ru']['EXIT_TAGS'], 'se nl')
            self.assertEqual(envs['ru']['ADMIN_SSH_PUBKEY'], PUBKEY)
            self.assertEqual(envs['ru']['NL_HY2_DIRECT_PORT'], '8443')
            self.assertEqual(envs['se']['SSH_HOST'], '192.0.2.2')

    def test_same_result_with_and_without_list_indent(self):
        indented = SAFE_DUMP.split('exits:')[0] + """exits:
  - host: 192.0.2.2
    password: exit pass
    tag: se
  - host: 192.0.2.3
    hy2_direct_port: 8443
    password: p2
    tag: nl
"""
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.assertEqual(self.convert(SAFE_DUMP, a).returncode, 0)
            self.assertEqual(self.convert(indented, b).returncode, 0)
            self.assertEqual(self.envs(a), self.envs(b))

    def test_errors_in_unindented_list_name_key_or_line(self):
        cases = [
            (SAFE_DUMP.replace('  tag: nl', '  tga: nl'), 'exits[1].tga: неизвестный ключ'),
            (SAFE_DUMP.replace('- host: 192.0.2.3', '- host: "$(touch CANARY)"'), 'exits[1].host'),
            ('- tag: de\n' + SAFE_DUMP, 'yaml: expected <block end>, but found \'?\', строка 2'),
        ]
        for config, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                result = self.convert(config, directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse((Path(directory) / 'CANARY').exists())
                self.assertFalse((Path(directory) / 'infra/envs').exists())

    def test_folded_and_escaped_scalars(self):
        cases = [
            ("  password: 'first\n    second'\n", 'first second'),
            ('  password: "first\\\n    second"\n', 'firstsecond'),
            ('  password: "first\n\n    second"\n', 'first\nsecond'),
            ('  password: "\\u0451\\x41\\ \\t"\n', '\u0451A \t'),
        ]
        for line, expected in cases:
            config = SAFE_DUMP.replace('  password: entry pass\n', line)
            with self.subTest(line=line), tempfile.TemporaryDirectory() as directory:
                result = self.convert(config, directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                probe = subprocess.run(['bash', '-c', 'source infra/envs/ru.env; printf %s "$SSH_PASS"'],
                                       cwd=directory, capture_output=True, text=True, timeout=5)
                self.assertEqual(probe.stdout, expected)

    def test_set_port_finds_tag_anywhere_in_item(self):
        for tag, port in (('se', '2083'), ('nl', '2087')):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as directory:
                (Path(directory) / 'config.yaml').write_text(SAFE_DUMP)
                result = subprocess.run([sys.executable, str(ROOT / 'infra/ansible/inventory.py'), '--set-port', tag, port],
                                        capture_output=True, text=True, timeout=10, check=False,
                                        env={'VPN2_CONFIG': f'{directory}/config.yaml', 'VPN2_STATE_DIR': f'{directory}/state',
                                             'PYTHONPATH': str(ROOT / 'infra/vpn2/src'), 'PATH': '/usr/bin:/bin'})
                self.assertEqual(result.returncode, 0, result.stderr)
                cfg = parse_yaml((Path(directory) / 'config.yaml').read_text())
                ports = {entry['tag']: entry.get('hy2_direct_port') for entry in cfg['exits']}
                other = {'se': 'nl', 'nl': 'se'}[tag]
                self.assertEqual(ports[tag], port)
                self.assertEqual(ports[other], {'se': None, 'nl': '8443'}[other])

    def test_setup_merge_keeps_unindented_exits(self):
        answers = ("entry:\n  host: 192.0.2.1\n  password: 'new'\n"
                   "exits:\n  - tag: nl\n    host: 192.0.2.3\n    password: 'p3'\n")
        merged = merge_setup_config(SAFE_DUMP, answers)
        cfg = parse_yaml(merged)
        self.assertEqual([(e['tag'], e['password'], e.get('hy2_direct_port')) for e in cfg['exits']],
                         [('nl', 'p3', '8443')])
        self.assertEqual(cfg['admin']['ssh_pubkey'], PUBKEY)


if __name__ == '__main__':
    unittest.main()
