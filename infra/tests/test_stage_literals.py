"""Run real stage fragments; configuration values must stay literal data."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

from adguard_support import Hub
from containers_support import compose
from vpn2 import containers
from zapret_support import render


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
VALUES = ['operator"name', "operator'name", r'operator\name',
          'operator$HOME$(touch INJECTED)', 'operator\nname\n']


def source(stage):
    return (ROOT / 'infra/scripts' / stage).read_text()


def run(script, directory, **values):
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                          cwd=directory, env=dict(os.environ, **values),
                          text=True, capture_output=True, timeout=15)


class AdguardLiteralTests(unittest.TestCase):
    def test_fresh_credentials(self):
        for value in [*VALUES, 'true', 'name: value', '*alias']:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                hub = Hub(work, user=value)
                hub.deploy()
                cfg = yaml.safe_load(hub.config.read_text())
                self.assertEqual(cfg['users'], [{'name': value, 'password': 'hash'}])
                self.assertEqual(hub.config.stat().st_mode & 0o777, 0o600)

    def test_sync_credentials(self):
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                # Имя проходит весь путь: config.yaml, config2env, env стадии, конфиг AdGuard.
                config = ('entry:\n  host: entry.example.test\n  password: fake\n'
                          'exits:\n  - tag: test\n    host: exit.example.test\n'
                          '    password: fake\nadmin:\n  user: ' + json.dumps(value) + '\n')
                (Path(work) / 'config.yaml').write_text(config)
                converted = subprocess.run([sys.executable, str(ROOT / 'infra/lib/config2env.py'), work],
                                           text=True, capture_output=True, timeout=15,
                                           env={**os.environ, 'VPN2_STATE_DIR': f'{work}/state'})
                self.assertEqual(converted.returncode, 0, converted.stderr)
                user = run('source infra/envs/ru.env\nprintf %s "$ADMIN_USER"', work)
                self.assertEqual(user.stdout, value, user.stderr)
                hub = Hub(Path(work, 'hub'), user=user.stdout)
                hub.config.write_text(yaml.safe_dump({'users': [], 'custom': 'keep'}))
                hub.deploy()
                cfg = yaml.safe_load(hub.config.read_text())
                self.assertEqual(cfg['users'], [{'name': value, 'password': 'hash'}])
                self.assertEqual(cfg['custom'], 'keep')
                self.assertTrue(hub.changed)
                before = hub.config.read_bytes()
                hub.deploy()
                self.assertFalse(hub.changed)
                self.assertEqual(before, hub.config.read_bytes())
                self.assertFalse((Path(work) / 'INJECTED').exists())


class YoutubeLiteralTests(unittest.TestCase):
    def test_generated_diagnostic_shell(self):
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                check = render('anysda-yt-check', base=value, diag_uid=123, diag_gid=123)
                check = check.replace('/proc/net/netfilter/nfnetlink_queue', work + '/queue')
                (Path(work) / 'check.sh').write_text(check)
                (Path(work) / 'queue').write_text('200 123 0 2 65531 0 0 1 1\n')
                # Очередь готова и счётчик растёт: вердикт решают коды HTTP.
                checked = run('curl() { printf 000; }; setpriv() { printf 000; }; '
                              'systemctl() { [[ $1 == is-active ]] || echo 123; }; '
                              'nft() { n=$(cat n 2>/dev/null || echo 0); echo $((n + 1)) > n; '
                              'printf \'{"nftables":[{"rule":{"expr":[{"counter":{"packets":%s}},'
                              '{"queue":{"num":200}}]}}]}\' "$n"; }; '
                              'export -f curl setpriv systemctl nft; bash check.sh', work)
                self.assertEqual(checked.returncode, 1, checked.stderr)
                self.assertIn('  подбор стратегии: ' + value + '/blockcheck2.sh\n', checked.stdout)
                self.assertFalse((Path(work) / 'INJECTED').exists())


class FrontendLiteralTests(unittest.TestCase):
    def test_management_pairs(self):
        for value in [*VALUES, '*', 'two  words']:
            with self.subTest(value=value):
                environ = {'EXIT_TAGS': 'test tail', 'MGMT_IP_RU': '192.0.2.1', 'MGMT_IP_TEST': value,
                           'MGMT_IP_TAIL': '192.0.2.3'}
                self.assertEqual(compose.mgmt_ips(environ), f'ru:192.0.2.1,test:{value},tail:192.0.2.3')


class LogLiteralTests(unittest.TestCase):
    def test_all_stage_log_prefixes(self):
        for path in sorted((ROOT / 'infra/scripts').glob('*.sh')):
            # Execute the actual filter, retaining its line selection and indentation.
            filters = re.findall(r'\| (sed(?: -n)? "[^"\n]*\$HOST_TAG[^"\n]*"|'
                                 r'HOST_TAG="\$HOST_TAG" awk \'[^\'\n]*\')', path.read_text())
            for command in filters:
                with tempfile.TemporaryDirectory() as work:
                    script = "printf 'one\\nfile.srs\\nthree\\n' | " + command
                    normal = run(script, work, HOST_TAG='plain')
                    self.assertEqual(normal.returncode, 0, normal.stderr)
                    for value in [*VALUES, 'tag/with&delimiter']:
                        with self.subTest(stage=path.name, command=command, value=value):
                            result = run(script, work, HOST_TAG=value)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(result.stdout, normal.stdout.replace('[plain]', '[' + value + ']'))


class BackupLiteralTests(unittest.TestCase):
    def test_archive_saved_with_warning_does_not_fail_unit(self):
        # Код 4 anysda-backup.sh — архив снят, предупреждение в журнале и в панели.
        unit = (ROOT / 'infra/ansible/roles/backup/templates/anysda-backup.service.j2').read_text()
        self.assertIn('\nSuccessExitStatus=4\n', unit)
        # backup.env пишется в синтаксисе Bash, для EnvironmentFile он не годится.
        self.assertNotIn('\nEnvironmentFile=', unit)


class TelegramLiteralTests(unittest.TestCase):
    def test_runtime_json(self):
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                path = Path(work) / 'telegram-runtime.json'
                with mock.patch.object(containers, 'ETC', work):
                    containers.sync_runtime({'TELEGRAM_BOT_TOKEN': value, 'TELEGRAM_CHAT_ID': value,
                                             'TELEGRAM_ADMIN_USERNAME': value}, [])
                self.assertEqual(json.loads(path.read_text()),
                                 dict(bot_token=value, chat_id=value, admin_username=value))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertFalse((Path(work) / 'INJECTED').exists())


if __name__ == '__main__':
    unittest.main()
