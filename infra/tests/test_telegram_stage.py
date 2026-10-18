"""Run complete stages with a stateful Docker double (restart keeps env).

VPN2_TEST_ROOT selects an old checkout without changing these assertions.
The live installation additionally exercises actual Docker and HTTP auth.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from test_deploy import ROOT


DOCKER = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
path = Path(os.environ['DOCKER_STATE'])
state = json.loads(path.read_text())
args = sys.argv[1:]
state['calls'].append(args)
containers = state['containers']
rc = 0
if args[0] == 'ps':
    wanted = next(a for a in args if a.startswith('name=')).split('=', 1)[1].strip('^$')
    if wanted in containers:
        print(wanted)
elif args[0] == 'inspect':
    container = containers[args[-1]]
    print(container['image'] if args[2] == '{{.Image}}' else json.dumps(container['env']))
elif args[:2] == ['image', 'inspect']:
    print(args[-1])
elif args[0] == 'pull':
    pass
elif args[0] == 'rm':
    containers.pop(args[-1], None)
elif args[0] == 'run':
    name = args[args.index('--name') + 1]
    if state.get('fail_panel') and name == 'anysda-vpn2':
        rc = 1
    else:
        # The panel starts through `IMAGE sh -ec '...'` (TLS Secure flag wrapper).
        image = args[args.index('sh') - 1] if 'sh' in args else args[-1]
        containers[name] = {'image': image,
                            'env': [args[i + 1] for i, a in enumerate(args) if a == '-e']}
elif args[0] == 'logs':
    if args[-1] == 'anysda-tgbot':
        print('Event server on 127.0.0.1:8877')
elif args[0] != 'restart':
    raise SystemExit('unexpected docker command: ' + repr(args))
path.write_text(json.dumps(state))
raise SystemExit(rc)
'''


class TelegramStageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='telegram-stage-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / 'docker.json'
        self.state.write_text(json.dumps({'containers': {
            'anysda-vpn2': {'image': 'sha256:installed-panel', 'env': ['NUXT_TGBOT_SECRET=']},
        }, 'calls': []}))
        scripts = self.root / 'scripts'
        scripts.mkdir()
        for name in ('30-frontend.sh', '35-telegram.sh'):
            stage = (ROOT / 'infra/scripts' / name).read_text()
            for prefix in ('/etc/', '/var/', '/opt/', '/tmp/anysda', '/usr/local/lib/'):
                stage = stage.replace(prefix, str(self.root) + prefix)
            (scripts / name).write_text(stage)
        (scripts / 'vpn2_tls.py').write_text((ROOT / 'infra/lib/vpn2_tls.py').read_text())
        (scripts / 'entry-resources.sh').write_text('entry_resources() { ENTRY_DOCKER_ARGS=(); }\n')
        # Release transactions have their own failure/rollback suite. This
        # fixture isolates bot/panel secret propagation with a Docker double.
        (scripts / 'release.sh').write_text((ROOT / 'infra/lib/release.sh').read_text())
        (scripts / 'panel-transaction.sh').write_text('panel_begin() { :; }\npanel_finish() { :; }\npanel_guard_files() { :; }\n')
        (scripts / 'anysda-svc.sh').write_text(
            f'source {shlex.quote(str(ROOT / "infra/lib/anysda-svc.sh"))}\n'
            'svc_restart_if_changed() { :; }\n')
        for name in ('etc/caddy', 'etc/systemd/system', 'tmp/anysda'):
            (self.root / name).mkdir(parents=True)
        (self.root / 'tmp/anysda/anysda-config.yaml.tpl').write_text('admin: fixture\n')
        binaries = self.root / 'bin'
        binaries.mkdir()
        commands = {'docker': DOCKER, 'caddy': '#!/bin/sh\necho v2.11.7\n',
                    'ufw': '#!/bin/sh\nexit 0\n', 'curl': '#!/bin/sh\nexit 0\n',
                    'envsubst': '#!/bin/sh\ncat\n', 'systemctl': '#!/bin/sh\nexit 0\n',
                    'journalctl': '#!/bin/sh\nexit 0\n'}
        for name, body in commands.items():
            path = binaries / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(binaries) + ':' + os.environ['PATH'],
                        DOCKER_STATE=str(self.state), HOST_TAG='ru', ENTRY_HOST='192.0.2.1',
                        AGH_PORT='3000', MGMT_IP='192.0.2.1', EXIT_TAGS='',
                        TELEGRAM_BOT_TOKEN='123456:test-only', TELEGRAM_CHAT_ID='100',
                        TELEGRAM_ADMIN_USERNAME='operator', TGBOT_IMAGE_PULL='false',
                        VPN2_DEVELOPMENT='true', TLS_INITIAL_WAIT='0')

    def run_stage(self, **env):
        result = subprocess.run(['bash', str(self.root / 'scripts/35-telegram.sh')],
                                env=self.env | env, text=True, capture_output=True, timeout=20)
        self.assertNotIn('123456:test-only', result.stdout + result.stderr)
        return result

    def ok(self, **env):
        result = self.run_stage(**env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def data(self):
        return json.loads(self.state.read_text())

    def container_env(self, name):
        return dict(item.split('=', 1) for item in self.data()['containers'][name]['env'])

    def test_enable_rotate_repeat_disable_reenable(self):
        self.ok()
        secret_file = self.root / 'etc/anysda/tgbot-secret.txt'
        secret = secret_file.read_text()
        self.assertEqual(len(secret), 32)
        panel_secret = self.container_env('anysda-vpn2')['NUXT_TGBOT_SECRET']
        self.assertEqual(len(panel_secret), 32)
        self.assertTrue(panel_secret == secret, 'panel and bot secrets differ')
        self.assertEqual(self.container_env('anysda-tgbot')['TGBOT_SECRET'], secret)
        self.assertEqual(self.data()['containers']['anysda-vpn2']['image'], 'sha256:installed-panel')
        self.assertFalse(any(call[0] == 'pull' for call in self.data()['calls']))
        calls = len(self.data()['calls'])
        self.ok()
        self.assertEqual(secret_file.read_text(), secret)
        self.assertFalse(any(call[0] in ('run', 'rm', 'restart') and 'anysda-vpn2' in call
                             for call in self.data()['calls'][calls:]))
        secret_file.write_text('replacement-test-secret')
        self.ok()
        self.assertEqual(self.container_env('anysda-vpn2')['NUXT_TGBOT_SECRET'], 'replacement-test-secret')
        self.assertEqual(self.container_env('anysda-tgbot')['TGBOT_SECRET'], 'replacement-test-secret')
        self.ok(TELEGRAM_BOT_TOKEN='')
        self.assertNotIn('anysda-tgbot', self.data()['containers'])
        self.assertEqual(self.container_env('anysda-vpn2')['NUXT_TGBOT_SECRET'], '')
        self.assertFalse(secret_file.exists())
        self.assertFalse((self.root / 'etc/anysda/telegram-runtime.json').exists())
        self.ok(TELEGRAM_BOT_TOKEN='')
        self.ok()
        self.assertEqual(len(self.container_env('anysda-vpn2')['NUXT_TGBOT_SECRET']), 32)

    def test_config_changes_reach_runtime_without_overwriting_unchanged_ui_fields(self):
        self.ok()
        runtime = self.root / 'etc/anysda/telegram-runtime.json'
        custom = dict(bot_token='987654:ui-only', chat_id='200', admin_username='ui_operator')
        runtime.write_text(json.dumps(custom))
        self.ok()
        self.assertEqual(json.loads(runtime.read_text()), custom)
        self.ok(TELEGRAM_CHAT_ID='300', TELEGRAM_ADMIN_USERNAME='new_operator')
        self.assertEqual(json.loads(runtime.read_text()), custom | dict(chat_id='300', admin_username='new_operator'))
        self.ok(TELEGRAM_BOT_TOKEN='765432:new-token', TELEGRAM_CHAT_ID='300',
                TELEGRAM_ADMIN_USERNAME='new_operator')
        self.assertEqual(json.loads(runtime.read_text())['bot_token'], '765432:new-token')

    def test_panel_failure_is_not_success_and_bot_does_not_start(self):
        self.state.write_text(json.dumps(self.data() | {'fail_panel': True}))
        result = self.run_stage()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('anysda-tgbot', self.data()['containers'])
        self.assertFalse((self.root / 'var/anysda/.stamps/35-telegram').exists())

    def test_fresh_install_without_panel_and_empty_secret(self):
        data = self.data()
        data['containers'] = {}
        self.state.write_text(json.dumps(data))
        secret = self.root / 'etc/anysda/tgbot-secret.txt'
        secret.parent.mkdir(parents=True)
        secret.touch()
        self.ok()
        self.assertEqual(len(self.container_env('anysda-tgbot')['TGBOT_SECRET']), 32)
        self.assertNotIn('anysda-vpn2', self.data()['containers'])


if __name__ == '__main__':
    unittest.main()
