"""config.yaml -> значения узлов (`inventory.py --prepare/--env`): отказы с путём ключа до записей, формы YAML, shell-безопасность."""
from pathlib import Path
import tempfile
import unittest

from envs import node_env, node_envs, parse, sourced
from vpn2 import config as vpn2_config
from vpn2.containers import compose
from vpn2.setup import parse_yaml, update

CONFIG = """entry:
  host: 192.0.2.1
  password: 'entry secret'
exits:
  - tag: se
    host: 192.0.2.2
    password: 'exit secret'
"""
# Дословный вывод yaml.safe_dump: ключи по алфавиту, список без отступа, длинная строка перенесена на 80 символах.
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
PUBKEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBkCV2mrwNtd8HZsr2fO8QTtR3dQ5Q2sL8kQ5Wx9h4yZ user@host'
# Секреты и решения стадии, которых нет в значениях узла.
FACTS = {'session': 's', 'clash': 'c', 'tgbot': 't', 'admin_user': 'admin', 'agh_password': 'p',
         'sso_enabled': 'false', 'sso_auto_redirect': 'false', 'sso_password_login': 'true', 'sso_redirect_url': ''}


def backup(enabled, passphrase_line=None):
    return CONFIG + f'backup:\n  enabled: {enabled}\n  backend: local\n' + (f'  {passphrase_line}\n' if passphrase_line else '')


def rejected():
    """(config.yaml, путь ключа в отказе) - отказ до записей, без трассировки и без значения секрета."""
    cases = [(CONFIG + 'ports:\n  hy2_direct: "443; touch CANARY"\n', 'ports.hy2_direct'),
             (CONFIG.replace('host: 192.0.2.2', 'host: "$(touch CANARY)"'), 'exits[0].host'),
             (CONFIG.replace('tag: se', 'tag: "$(touch CANARY)"'), 'exits[0].tag'),
             (CONFIG.replace('tag: se', 'tag: ../escaped'), 'exits[0].tag'),
             (CONFIG + '  - tag: se\n    host: 192.0.2.3\n    password: x\n', 'exits[1].tag'),
             (CONFIG.split('exits:')[0] + 'exits:\n', 'exits'),
             (SAFE_DUMP.replace('  tag: nl', '  tga: nl'), 'exits[1].tga: неизвестный ключ'),
             (SAFE_DUMP.replace('- host: 192.0.2.3', '- host: "$(touch CANARY)"'), 'exits[1].host'),
             ('- tag: de\n' + SAFE_DUMP, "yaml: expected <block end>, but found '?', строка 2")]
    for value in ('0', '65536', '-1', '1.5', 'true', "''"):
        cases += [(CONFIG + f'ports:\n  hy2_warp: {value}\n', 'ports.hy2_warp'),
                  (CONFIG + f'    hy2_direct_port: {value}\n', 'exits[0].hy2_direct_port')]
    cases += [(CONFIG.replace('tag: se', f'tag: {tag}'), 'exits[0].tag')
              for tag in ('ru', 'all', 'exits', 'foreign', 'SE', '-us', 'us-', 'US', 'us_2')]
    for enabled in ('true', 'yes'):
        cases += [(backup(enabled, line), 'backup.passphrase')
                  for line in ("passphrase: ''", 'passphrase: ""', 'passphrase:')]
    cases.append((backup('true'), 'backup.passphrase'))
    cases += [(CONFIG + extra, key) for extra, key in (
        ('sso:\n  enabeld: true\n', 'sso.enabeld'), ('panle:\n  domain: vpn.example.com\n', 'panle'),
        ('ports:\n  hy2_driect: 4443\n', 'ports.hy2_driect'), ('sso:\n  enabled: tru\n', 'sso.enabled'),
        ('sso:\n  password_login: flase\n', 'sso.password_login'), ('backup:\n  enabled: flase\n', 'backup.enabled'),
        ('backup:\n  s3: wrong\n', 'backup.s3'), ('admin:\n  password: [one, two]\n', 'admin.password'),
        ('admin:\n  password: null\n', 'admin.password'), ('admin:\n  password: true\n', 'admin.password'),
        ('admin:\n  password: "secret"junk\n', 'admin.password'), ('admin:\n  password: "secret\\q"\n', 'admin.password'),
        ('admin:\n  password: "unclosed\n', 'admin.password'), ('admin:\n  password: |\n    secret\n', 'admin.password'),
        ('admin:\n    password: secret\n', 'admin.password'), ('entry:\n  host: 192.0.2.4\n', 'entry'),
        ('panel:\n  domain: one.test\n  domain: two.test\n', 'panel.domain'), ('panel: wrong\n', 'panel'),
        ('entry_resources:\n  vm_memroy_mib: 512\n', 'entry_resources.vm_memroy_mib'),
        ('entry_resources:\n  adaway_enabled: yes\n', 'entry_resources.adaway_enabled'),
        ('entry_resources:\n  retention_days: 0\n', 'entry_resources.retention_days'),
        ('entry_resources:\n  querylog_size: 12x\n', 'entry_resources.querylog_size'),
        ('wireguard:\n  split_local: flase\n', 'wireguard.split_local'))]
    cases += [(CONFIG + 'panel:\n  domain: "' + domain.replace('"', '\\"') + '"\n', 'panel.domain')
              for domain in ('vpn.example.com\\n:80 { respond owned }', 'two names.test', '*.example.com',
                             'https://example.com', '-bad.example', 'bad..example', '"bad".example')]
    for key in ('wireguard', 'openvpn'):
        cases += [(CONFIG + f'ports:\n  {key}: {value}\n', f'ports.{key}')
                  for value in ('0', '65536', '-1', '1.5', 'true', "''", '"1; touch CANARY"')]
    cases += [(CONFIG + f'openvpn:\n  proto: {value}\n', 'openvpn.proto')
              for value in ('sctp', 'TCP', 'tcp-client', "''", '"udp\\ntcp"')]
    cases += [(CONFIG + f'wireguard:\n  mtu: {value}\n', 'wireguard.mtu')
              for value in ('1279', '9001', '-1', '1.5', "''", 'true')]
    cases.append(("entry:\n  password: 'CANARY_UNCLOSED_SECRET\n", 'ERROR: config.yaml:'))
    return cases


class Case(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def env(self, text, node='ru', **env):
        prepared, values = node_env(self.root, text, node, env={'HOME': str(self.root), **env})
        self.assertIsNotNone(values, prepared.stderr)
        return values

    def envs(self, text):
        prepared, texts = node_envs(self.root, text, env={'HOME': str(self.root)})
        self.assertTrue(texts, prepared.stderr)
        return texts


class RejectedTests(unittest.TestCase):
    def test_bad_config_is_refused_before_any_write(self):
        for text, key in rejected():
            with self.subTest(key=key, config=text), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'state').mkdir()
                (root / 'state/mgmt-ips.txt').write_text('KEEP\n')
                prepared, texts = node_envs(root, text, env={'HOME': directory})
                self.assertNotEqual(prepared.returncode, 0)
                self.assertEqual(texts, {})
                self.assertIn(key, prepared.stderr)
                self.assertNotIn('Traceback', prepared.stderr)
                for secret in ('secret', 'CANARY_UNCLOSED_SECRET'):
                    self.assertNotIn(secret, prepared.stdout + prepared.stderr.replace(key, ''))
                self.assertFalse((root / 'CANARY').exists(), 'shell injection')
                self.assertEqual((root / 'state/mgmt-ips.txt').read_text(), 'KEEP\n')


class AcceptedTests(Case):
    def test_exit_tag_syntax(self):
        for tag in ('a', 'us2', 'us-2', 'us--2'):
            with self.subTest(tag=tag):
                self.assertIn(tag, self.envs(CONFIG.replace('tag: se', f'tag: {tag}')))

    def test_boundary_ports_ipv6_and_shell_quoted_secrets(self):
        secret = " spaces ' \" $HOME $(touch CANARY)\\nsecond line "
        text = CONFIG.replace("'entry secret'", '"' + secret.replace('"', '\\"') + '"').replace('192.0.2.2', '2001:db8::2')
        result = sourced(self.root, self.envs(text + 'ports:\n  hy2_direct: 1\n  hy2_warp: 65535\n')['ru'],
                         'printf "%s" "$SSH_PASS"')
        self.assertEqual((result.returncode, result.stdout), (0, secret.replace('\\n', '\n')), result.stderr)
        self.assertFalse((self.root / 'CANARY').exists())

    def test_safe_dump_output_is_accepted(self):
        envs = {node: parse(text) for node, text in self.envs(SAFE_DUMP).items()}
        self.assertEqual((envs['ru']['EXIT_TAGS'], envs['ru']['ADMIN_SSH_PUBKEY'], envs['ru']['NL_HY2_DIRECT_PORT'],
                          envs['se']['SSH_HOST']), ('se nl', PUBKEY, '8443', '192.0.2.2'))

    def test_same_result_with_and_without_list_indent(self):
        indented = SAFE_DUMP.split('exits:')[0] + 'exits:\n' + ''.join(
            '  ' + line + '\n' for line in SAFE_DUMP.split('exits:\n')[1].splitlines())
        self.assertEqual(self.envs(SAFE_DUMP), self.envs(indented))

    def test_folded_and_escaped_scalars(self):
        for line, expected in (("  password: 'first\n    second'\n", 'first second'),
                               ('  password: "first\\\n    second"\n', 'firstsecond'),
                               ('  password: "first\n\n    second"\n', 'first\nsecond'),
                               ('  password: "\\u0451\\x41\\ \\t"\n', '\u0451A \t')):
            with self.subTest(line=line):
                text = self.envs(SAFE_DUMP.replace('  password: entry pass\n', line))['ru']
                self.assertEqual(sourced(self.root, text, 'printf %s "$SSH_PASS"').stdout, expected)

    def test_backup_passphrase(self):
        for line, value in (("passphrase: '   '", '   '), ('passphrase: "\t "', '\t '),
                            ("passphrase: 'long backup secret'", 'long backup secret')):
            with self.subTest(line=line):
                env = self.env(backup('true', line))
                self.assertEqual((env['BACKUP_ENABLED'], env['BACKUP_PASSPHRASE']), ('true', value))
        for text in (backup('false', "passphrase: ''"), backup('false'), CONFIG):
            with self.subTest(text=text):
                self.assertNotIn('BACKUP_ENABLED', ''.join(self.envs(text).values()))

    def test_entry_resources_bandwidth_and_window_scale(self):
        self.env(CONFIG + 'entry_resources:\n  querylog_size: 0\n  adaway_enabled: false\n'
                 '  retention_days: "11"\n  vm_memory_mib: 384\n')
        texts = self.envs(CONFIG + '    hy2_up_mbps: 300\n    hy2_down_mbps: 900\nyoutube:\n  route: zapret\n'
                          '  window_scale: 1\n')
        self.assertIn('YT_ZAPRET_WINDOW_SCALE=', ''.join(texts.values()))
        self.assertIn('SE_HY2_UP_MBPS=', ''.join(texts.values()))

    def test_sso_seed_validation_is_deferred_to_entry(self):
        for seed in ('  base_url: https://auth.example.test\n  allowed_subs: test-sub\n', ''):
            with self.subTest(seed=seed):
                env = self.env(CONFIG + 'sso:\n  enabled: true\n  client_secret: ""\n' + seed)
                self.assertEqual([env.get(name) for name in ('SSO_REQUESTED', 'SSO_ENABLED', 'SSO_AUTO_REDIRECT',
                                                              'SSO_PASSWORD_LOGIN', 'SSO_CLIENT_SECRET')],
                                 ['true', 'false', 'false', 'true', ''])


class SetPortTests(unittest.TestCase):
    def test_set_port_finds_tag_anywhere_in_item(self):
        for tag, port, other, kept in (('se', '2083', 'nl', '8443'), ('nl', '2087', 'se', None)):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'config.yaml'
                path.write_text(SAFE_DUMP)
                vpn2_config.set_exit_port(path, tag, port)
                ports = {entry['tag']: entry.get('hy2_direct_port') for entry in parse_yaml(path.read_text())['exits']}
                self.assertEqual((ports[tag], ports[other]), (port, kept))

    def test_setup_merge_keeps_unindented_exits(self):
        cfg = parse_yaml(update(SAFE_DUMP, {'entry': {'host': '192.0.2.1', 'password': "'new'"},
                                            'exits': [('nl', '192.0.2.3', 'p3')]}))
        self.assertEqual([(e['tag'], e['password'], e.get('hy2_direct_port')) for e in cfg['exits']],
                         [('nl', 'p3', '8443')])
        self.assertEqual(cfg['admin']['ssh_pubkey'], PUBKEY)


class PanelTests(Case):
    """Значения узла доходят до окружения панели в compose.yaml."""
    VPN = ('WG_LISTEN_PORT', 'OVPN_PORT', 'OVPN_PROTO', 'WG_MTU', 'WG_SPLIT_LOCAL')

    def panel(self, text):
        result = sourced(self.root, self.envs(text)['ru'], 'env -0')
        environ = dict(item.split('=', 1) for item in result.stdout.split('\0') if item)
        return environ, compose.panel_environment({'AGH_PORT': '3000', **environ}, FACTS)

    def test_settings_reach_node_and_panel(self):
        environ, panel = self.panel(CONFIG + 'ports:\n  wireguard: 51830\n  openvpn: 1195\nopenvpn:\n  proto: tcp\n'
                                    'wireguard:\n  mtu: 1380\n  split_local: false\n')
        values = ['51830', '1195', 'tcp', '1380', 'false']
        self.assertEqual([environ[name] for name in self.VPN], values)
        self.assertEqual([panel['NUXT_' + name] for name in self.VPN], values)

    def test_omitted_keys_leave_role_defaults(self):
        for extra in ('', 'wireguard:\nopenvpn:\n'):
            with self.subTest(extra=extra):
                self.assertNotRegex(self.envs(CONFIG + extra)['ru'], r'(?m)^(WG_LISTEN_PORT|OVPN_PORT|OVPN_PROTO|WG_MTU)=.')

    def test_supported_protocols(self):
        for proto in ('udp', 'udp4', 'udp6', 'tcp', 'tcp4', 'tcp6', 'tcp-server', 'tcp4-server', 'tcp6-server'):
            with self.subTest(proto=proto):
                self.assertEqual(self.env(CONFIG + f'openvpn:\n  proto: {proto}\n')['OVPN_PROTO'], proto)

    def test_numeric_boundaries_and_boolean_normalization(self):
        for port, mtu, split, expected in (('1', '1280', 'yes', 'true'), ('65535', '9000', '0', 'false'),
                                           ('051830', '01380', 'FALSE', 'false')):
            with self.subTest(port=port):
                env = self.env(CONFIG + f'ports:\n  wireguard: {port}\n  openvpn: {port}\n'
                               f'wireguard:\n  mtu: {mtu}\n  split_local: {split}\n')
                self.assertEqual([env[name] for name in ('WG_LISTEN_PORT', 'OVPN_PORT', 'WG_MTU', 'WG_SPLIT_LOCAL')],
                                 [str(int(port)), str(int(port)), str(int(mtu)), expected])

    def test_domain_normalization(self):
        for domain, expected in (('VPN.Example.COM', 'vpn.example.com'), ('ПРИМЕР.РФ', 'xn--e1afmkfd.xn--p1ai')):
            with self.subTest(domain=domain):
                text = self.envs(CONFIG + f'panel:\n  domain: {domain}\n')['ru']
                self.assertEqual(sourced(self.root, text, 'printf %s "$PANEL_DOMAIN"').stdout, expected)

    def test_client_configs_use_panel_domain_unless_public_host_is_set(self):
        for extra, expected in (('', '192.0.2.1'), ('panel:\n  domain: VPN.Example.COM\n', 'vpn.example.com')):
            for public in ('', 'edge.example.net'):
                text = CONFIG + extra
                if public:
                    text = text.replace("password: 'entry secret'", f"password: 'entry secret'\n  public_host: {public}", 1)
                with self.subTest(extra=extra, public=public):
                    _, panel = self.panel(text)
                    self.assertEqual((panel['NUXT_WG_PUBLIC_HOST'], panel['NUXT_OVPN_PUBLIC_HOST']),
                                     (public or expected,) * 2)
