"""Run real stage fragments; configuration values must stay literal data."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import yaml


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
        text = source('22-adguard.sh')
        block = text[text.index('agh_fresh=()'):text.index('\nelse\n  # Config exists')]
        for value in [*VALUES, 'true', 'name: value', '*alias']:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                path = Path(work) / 'fresh.yaml'
                result = run('svc_forget() { :; }\n' + block + '\nfi', work,
                             AGH_CFG=str(path), AGH_USER=value, AGH_PASS_HASH='test-hash',
                             AGH_PORT='3000', MGMT_IP='192.0.2.1',
                             AGH_ADAWAY='true', AGH_QUERYLOG='1000')
                self.assertEqual(result.returncode, 0, result.stderr)
                cfg = yaml.safe_load(path.read_text())
                self.assertEqual(cfg['users'], [{'name': value, 'password': 'test-hash'}])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sync_credentials(self):
        text = source('22-adguard.sh')
        block = text[text.index('  agh_sync=$('):text.index('\npatch_agh_config "$AGH_CFG"')]
        block = block.rsplit('\nfi', 1)[0]
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                path = Path(work) / 'config"\\$.yaml'
                path.write_text(yaml.safe_dump({'users': [], 'custom': 'keep'}))
                # Exercise config2env as well as source of its shell assignments.
                config = ('entry:\n  host: entry.example.test\n  password: fake\n'
                          'exits:\n  - tag: test\n    host: exit.example.test\n'
                          '    password: fake\nadmin:\n  user: ' + json.dumps(value) + '\n')
                (Path(work) / 'config.yaml').write_text(config)
                converted = subprocess.run([sys.executable, str(ROOT / 'infra/lib/config2env.py'), work],
                                           text=True, capture_output=True, timeout=15)
                self.assertEqual(converted.returncode, 0, converted.stderr)
                script = ('source infra/envs/ru.env\nAGH_USER=$ADMIN_USER\n'
                          'svc_forget() { echo restart; }\n' + block)
                first = run(script, work, AGH_CFG=str(path), AGH_PASS_HASH=value, HOST_TAG='ru')
                self.assertEqual(first.returncode, 0, first.stderr)
                cfg = yaml.safe_load(path.read_text())
                self.assertEqual(cfg['users'], [{'name': value, 'password': value}])
                self.assertEqual(cfg['custom'], 'keep')
                self.assertIn('restart', first.stdout)
                before = path.read_bytes()
                second = run(script, work, AGH_CFG=str(path), AGH_PASS_HASH=value, HOST_TAG='ru')
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertNotIn('restart', second.stdout)
                self.assertEqual(before, path.read_bytes())
                self.assertFalse((Path(work) / 'INJECTED').exists())


class YoutubeLiteralTests(unittest.TestCase):
    def test_generated_diagnostic_shell(self):
        text = source('19-yt-zapret.sh')
        block = text[text.index('cat > /usr/local/bin/anysda-yt-check'):
                     text.index('chmod 0755 /usr/local/bin/anysda-yt-check')]
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                result = run('id() { printf 123; }\n' +
                             block.replace('/usr/local/bin/anysda-yt-check', 'check.sh'), work,
                             DIAG_USER='test', DIAG_UID='123', YT_MARK='256', YT_ZAPRET_BASE=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                checked = run('curl() { printf 000; }; setpriv() { printf 000; }; '
                              'export -f curl setpriv; bash check.sh', work)
                self.assertEqual(checked.returncode, 1, checked.stderr)
                self.assertIn('  подбор стратегии: ' + value + '/blockcheck2.sh\n', checked.stdout)
                self.assertFalse((Path(work) / 'INJECTED').exists())


class FrontendLiteralTests(unittest.TestCase):
    def test_management_pairs(self):
        text = source('30-frontend.sh')
        line = next(line for line in text.splitlines() if '-e NUXT_MGMT_IPS=' in line)
        expression = line.split('NUXT_MGMT_IPS=', 1)[1].removesuffix(' \\')
        for value in [*VALUES, '*', 'two  words']:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                (Path(work) / 'glob-match').touch()
                result = run('printf "%s" ' + expression, work, EXIT_TAGS='test tail',
                             MGMT_IP_RU='192.0.2.1', MGMT_IP_TEST=value, MGMT_IP_TAIL='192.0.2.3')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f'ru:192.0.2.1,test:{value},tail:192.0.2.3')


class RouterExportLiteralTests(unittest.TestCase):
    def test_default_ports_are_data(self):
        text = source('20-ru-router.sh')
        start = text.index('for _t in $EXIT_TAGS;', text.index('export YT_ROUTE='))
        block = text[start:text.index('\n# TLS pinning', start)]
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                result = run(block + '\nprintf "%s\\0" "$TEST_HY2_DIRECT_PORT" "$TEST_HY2_MGMT_PORT"',
                             work, EXIT_TAGS='test', HY2_DIRECT_PORT=value, HY2_MGMT_PORT=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.split('\0')[:-1], [value, value])
                self.assertFalse((Path(work) / 'INJECTED').exists())


class LogLiteralTests(unittest.TestCase):
    def test_all_stage_log_prefixes(self):
        count = 0
        for path in sorted((ROOT / 'infra/scripts').glob('*.sh')):
            # Execute the actual filter, retaining its line selection and indentation.
            filters = re.findall(r'\| (sed(?: -n)? "[^"\n]*\$HOST_TAG[^"\n]*"|'
                                 r'HOST_TAG="\$HOST_TAG" awk \'[^\'\n]*\')', path.read_text())
            for command in filters:
                count += 1
                with tempfile.TemporaryDirectory() as work:
                    script = "printf 'one\\nfile.srs\\nthree\\n' | " + command
                    normal = run(script, work, HOST_TAG='plain')
                    self.assertEqual(normal.returncode, 0, normal.stderr)
                    for value in [*VALUES, 'tag/with&delimiter']:
                        with self.subTest(stage=path.name, command=command, value=value):
                            result = run(script, work, HOST_TAG=value)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(result.stdout, normal.stdout.replace('[plain]', '[' + value + ']'))
        self.assertGreater(count, 20)


class IkeRoutingLiteralTests(unittest.TestCase):
    def test_generated_routing_shell(self):
        text = source('27-ikev2.sh')
        block = text[text.index('cat > /usr/local/sbin/anysda-ikev2-routing.sh'):
                     text.index('chmod +x /usr/local/sbin/anysda-ikev2-routing.sh')]
        for value in [*VALUES, "'; touch INJECTED; #"]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                result = run(block.replace('/usr/local/sbin/anysda-ikev2-routing.sh', 'routing.sh'),
                             work, WAN_IF=value, IKEV2_SUBNET=value + '/24', IKEV2_SUBNET6=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                # Unknown action executes assignments without touching networking.
                loaded = run('source routing.sh inspect\nprintf "%s\\0" '
                             '"$WAN_IF" "$IKEV2_SUBNET" "$IKEV2_SUBNET6"', work)
                self.assertEqual(loaded.returncode, 0, loaded.stderr)
                self.assertEqual(loaded.stdout.split('\0')[:-1], [value, value + '/24', value])
                self.assertFalse((Path(work) / 'INJECTED').exists())


class BackupLiteralTests(unittest.TestCase):
    def test_backup_env_roundtrip(self):
        text = source('26-backup.sh')
        block = text[text.index('echo "[$HOST_TAG] [3/4]'):text.index('# ── 4. Systemd')]
        keys = ['BACKUP_LOCAL_DIR', 'BACKUP_RETENTION', 'BACKUP_PASSPHRASE',
                'BACKUP_S3_ENDPOINT', 'BACKUP_S3_BUCKET', 'BACKUP_S3_REGION',
                'BACKUP_S3_ACCESS_KEY', 'BACKUP_S3_SECRET_KEY']
        for value in [*VALUES, "'; touch INJECTED; #"]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                path = Path(work) / 'backup.env'
                result = run(block.replace('/etc/anysda/backup.env', str(path)), work,
                             HOST_TAG='ru', BACKUP_BACKEND='s3', **dict.fromkeys(keys, value))
                self.assertEqual(result.returncode, 0, result.stderr)
                # Start with a clean shell so original env cannot hide a broken assignment.
                names = [key.replace('BACKUP_PASSPHRASE', 'ANYSDA_BACKUP_PASSPHRASE')
                         .replace('BACKUP_S3_ACCESS_KEY', 'ANYSDA_S3_ACCESS_KEY')
                         .replace('BACKUP_S3_SECRET_KEY', 'ANYSDA_S3_SECRET_KEY') for key in keys]
                loaded = run('source backup.env\nprintf "%s\\0" ' +
                             ' '.join('"$' + key + '"' for key in names), work)
                self.assertEqual(loaded.returncode, 0, loaded.stderr)
                self.assertEqual(loaded.stdout.split('\0')[:-1], [value] * len(keys))
                self.assertFalse((Path(work) / 'INJECTED').exists())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class TelegramLiteralTests(unittest.TestCase):
    def test_runtime_json(self):
        text = source('35-telegram.sh')
        start = text.index('if [[ ! -f /etc/anysda/telegram-runtime.json')
        block = text[start:text.index('\nfi', start) + 3]
        for value in VALUES:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as work:
                path = Path(work) / 'telegram-runtime.json'
                script = block.replace('/etc/anysda/telegram-runtime.json', str(path))
                result = run(script, work, TELEGRAM_BOT_TOKEN=value, TELEGRAM_CHAT_ID=value,
                             TELEGRAM_ADMIN_USERNAME=value, HOST_TAG='ru')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(path.read_text()),
                                 dict(bot_token=value, chat_id=value, admin_username=value))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                second = run(script, work, HOST_TAG='ru')
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(json.loads(path.read_text())['bot_token'], value)
                self.assertFalse((Path(work) / 'INJECTED').exists())


if __name__ == '__main__':
    unittest.main()
