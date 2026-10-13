"""Параметры VPN проходят от config.yaml до env сервера и панели."""
import hashlib
from pathlib import Path
import tempfile
import unittest

import test_config_validation as validation
from test_deploy import bash, function, source


class VpnConfigTests(unittest.TestCase):
    def convert(self, extra, directory):
        return validation.ConfigValidationTests.convert(self, validation.CONFIG + extra, directory)

    def test_settings_reach_remote_stage_and_panel(self):
        extra = ('ports:\n  wireguard: 51830\n  openvpn: 1195\n'
                 'openvpn:\n  proto: tcp\nwireguard:\n  mtu: 1380\n  split_local: false\n')
        with tempfile.TemporaryDirectory() as directory:
            result = self.convert(extra, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            root = Path(directory)
            (root / 'infra/scripts').mkdir()
            for stage in ('28-wireguard', '29-openvpn', '30-frontend'):
                (root / 'infra/scripts' / (stage + '.sh')).touch()
            result = bash(function('run_stage_on_host') + '''
DEPLOY_ROOT="$PWD/infra"
load_env() { :; }
log() { :; }
die() { return 1; }
push() {
  if [[ $1 == */rendered/ru.env ]]; then
    grep -E '^(WG_LISTEN_PORT|OVPN_PORT|OVPN_PROTO|WG_MTU|WG_SPLIT_LOCAL)=' "$1"
  fi
  return 1
}
for stage in 28-wireguard 29-openvpn 30-frontend; do
  run_stage_on_host "$stage" ru || :
done
''', directory)
            expected = ("WG_LISTEN_PORT='51830'\nOVPN_PORT='1195'\nOVPN_PROTO='tcp'\n"
                        "WG_MTU='1380'\nWG_SPLIT_LOCAL='false'\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected * 3)
            # Выполняем настоящие подстановки docker run, не запускаем Docker.
            frontend = '\n'.join(line.strip().removesuffix('\\') for line in
                                 source('infra/scripts/30-frontend.sh').splitlines()
                                 if '-e NUXT_' in line and any(key in line for key in
                                    ('WG_LISTEN_PORT=', 'OVPN_PORT=', 'OVPN_PROTO=',
                                     'WG_MTU=', 'WG_SPLIT_LOCAL=')))
            result = bash('source infra/secrets/rendered/ru.env\nprintf "%s\\n" '
                          + frontend.replace('\n', ' '), directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([line for line in result.stdout.splitlines() if line != '-e'], [
                'NUXT_WG_LISTEN_PORT=51830', 'NUXT_WG_MTU=1380',
                'NUXT_WG_SPLIT_LOCAL=false', 'NUXT_OVPN_PORT=1195', 'NUXT_OVPN_PROTO=tcp'])

    def test_omitted_keys_preserve_env_bytes_and_stage_defaults(self):
        # Получено на origin/dev для CONFIG; не обновлять при добавлении опций.
        expected = {
            'all.env': '6360e56a63cac531c6eb4737d1a5eaca59612a6f53906069ca74dd5135ef3c31',
            'exits.env': 'fe0370c84eb74bb8751dcb803d25743e379504b2dd1a144eeb8903cee5ea5c91',
            'ru.env': '07f5a3f4177ee1e74f476c36619c65ddb1bbd8ad4cfcf75eede7d0c67e08bb75',
            'se.env': '099a27f2e11ad743e64d85cc261f1eb4d91a664c5965c9b03a47d8c060f9fc25',
        }
        for extra in ('', 'wireguard:\nopenvpn:\n'):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                result = self.convert(extra, directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in (Path(directory) / 'infra/envs').glob('*.env')}
                self.assertEqual(actual, expected)
                assignments = '\n'.join(line for stage in ('28-wireguard', '29-openvpn')
                                        for line in source(f'infra/scripts/{stage}.sh').splitlines()
                                        if line.startswith(('WG_PORT=', 'OVPN_PORT=', 'OVPN_PROTO=')))
                result = bash('unset WG_LISTEN_PORT OVPN_PORT OVPN_PROTO\n'
                              'source infra/envs/all.env\nsource infra/envs/ru.env\n'
                              + assignments + '\nprintf "%s/%s/%s\\n" "$WG_PORT" "$OVPN_PORT" "$OVPN_PROTO"',
                              directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '51820/1194/udp\n')

    def test_supported_protocols(self):
        for proto in ('udp', 'udp4', 'udp6', 'tcp', 'tcp4', 'tcp6',
                      'tcp-server', 'tcp4-server', 'tcp6-server'):
            with self.subTest(proto=proto), tempfile.TemporaryDirectory() as directory:
                result = self.convert(f'openvpn:\n  proto: {proto}\n', directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                env = (Path(directory) / 'infra/envs/ru.env').read_text()
                self.assertIn(f"OVPN_PROTO='{proto}'\n", env)

    def test_numeric_boundaries_and_boolean_normalization(self):
        for port, mtu, split, expected in (('1', '1280', 'yes', 'true'),
                                           ('65535', '9000', '0', 'false'),
                                           ('051830', '01380', 'FALSE', 'false')):
            with self.subTest(port=port), tempfile.TemporaryDirectory() as directory:
                result = self.convert(f'ports:\n  wireguard: {port}\n  openvpn: {port}\n'
                                      f'wireguard:\n  mtu: {mtu}\n  split_local: {split}\n', directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                env = (Path(directory) / 'infra/envs/ru.env').read_text()
                for line in (f"WG_LISTEN_PORT='{int(port)}'", f"OVPN_PORT='{int(port)}'",
                             f"WG_MTU='{int(mtu)}'", f"WG_SPLIT_LOCAL='{expected}'"):
                    self.assertIn(line + '\n', env)

    def test_invalid_values_leave_existing_env_and_state_untouched(self):
        cases = []
        for key in ('wireguard', 'openvpn'):
            for value in ('0', '65536', '-1', '1.5', 'true', "''", '"1; touch CANARY"'):
                cases.append((f'ports:\n  {key}: {value}\n', f'ports.{key}'))
        for value in ('sctp', 'TCP', 'tcp-client', "''", '"udp\\ntcp"'):
            cases.append((f'openvpn:\n  proto: {value}\n', 'openvpn.proto'))
        for value in ('1279', '9001', '-1', '1.5', "''", 'true'):
            cases.append((f'wireguard:\n  mtu: {value}\n', 'wireguard.mtu'))
        cases.append(('wireguard:\n  split_local: flase\n', 'wireguard.split_local'))
        for extra, key in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for path in ('infra/envs/ru.env', 'infra/state/mgmt-ips.txt'):
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text('KEEP\n')
                result = self.convert(extra, directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual((root / 'infra/envs/ru.env').read_text(), 'KEEP\n')
                self.assertEqual((root / 'infra/state/mgmt-ips.txt').read_text(), 'KEEP\n')
                self.assertFalse((root / 'infra/envs/all.env').exists())


if __name__ == '__main__':
    unittest.main()
