"""Деплой сообщает о завершении и заранее тянет образ бота по токену из панели или config.yaml.

announce_deploy_done и prefetch_images - настоящие функции deploy.sh; команды
на узел выполняются локально во временном корне, где лежат файлы установленного узла.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from test_deploy import function, source


# Узел: команда ssh_exec выполняется локально, пути узла - во временном корне.
NODE_PATHS = ('/etc/anysda', '/var/lib/anysda', '/tmp/anysda')
RECORDER = '#!/bin/sh\nprintf "%s\\n" "$(basename "$0") $*" >> "$CALLS"\n'
TGBOT_IMAGE = 'ghcr.io/anysda/vpn2/tgbot:dev'


def deploy_functions():
    names = re.findall(r'^([A-Za-z_][A-Za-z0-9_]*)\(\) \{', source('infra/deploy.sh'), re.M)
    return ''.join(function(name) for name in names)


class TelegramDeployNoticeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='telegram-deploy-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.calls = self.root / 'calls.log'
        self.calls.touch()
        binaries = self.root / 'bin'
        binaries.mkdir()
        for name in ('curl', 'docker'):
            (binaries / name).write_text(RECORDER)
            (binaries / name).chmod(0o755)
        (self.root / 'deploy/envs').mkdir(parents=True)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('TELEGRAM_', 'TGBOT_'))}
        self.env |= dict(PATH=str(binaries) + ':' + os.environ['PATH'], CALLS=str(self.calls),
                         NODE_ROOT=str(self.root), DEPLOY_ROOT=str(self.root / 'deploy'),
                         VPN2_DEVELOPMENT='true')

    def node(self, runtime):
        """Установленный узел: секрет бота и runtime.json, как их оставляет стадия 35."""
        etc = self.root / 'etc/anysda'
        etc.mkdir(parents=True)
        (etc / 'tgbot-secret.txt').write_text('fixture-secret')
        if runtime is not None:
            stored = self.root / 'var/lib/anysda-vpn2/runtime/telegram-runtime.json'
            stored.parent.mkdir(parents=True)
            stored.write_text(json.dumps(runtime))
            (etc / 'telegram-runtime.json').symlink_to(stored)

    def config(self, **telegram):
        """envs/ru.env из config.yaml; пустой config - без строк telegram."""
        lines = ['HOST_TAG=ru'] + [f'{key}={value}' for key, value in telegram.items()]
        (self.root / 'deploy/envs/ru.env').write_text('\n'.join(lines) + '\n')

    def deploy(self, call):
        rewrite = ''.join(f'  p={path}; c="${{c//"$p"/$NODE_ROOT$p}}"\n' for path in NODE_PATHS)
        script = deploy_functions() + '''
load_env() { set -a; source "$DEPLOY_ROOT/envs/$1.env"; set +a; }
ssh_exec() {
  local c="$*"
''' + rewrite + '''  bash -c "$c"
}
log() { :; }
ok() { :; }
warn() { :; }
die() { echo "die: $*" >&2; exit 1; }
''' + call
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.root,
                                env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.calls.read_text()

    def announced(self):
        calls = self.deploy('announce_deploy_done\n')
        return [line for line in calls.splitlines()
                if line.startswith('curl ') and 'deploy_done' in line]

    def prefetched(self):
        calls = self.deploy('prefetch_images\nwait "$_PREFETCH_PID"\n')
        return [line for line in calls.splitlines()
                if line.startswith('docker pull') and TGBOT_IMAGE in line]

    def panel_bot(self):
        self.config()
        self.node(dict(bot_token='987654:panel-only', chat_id='200', admin_username=''))

    def test_panel_token_announces_deploy_done(self):
        """Край 3: config пуст, токен в runtime узла - «Установка завершена» уходит."""
        self.panel_bot()
        sent = self.announced()
        self.assertEqual(len(sent), 1, self.calls.read_text())
        self.assertIn('X-Tgbot-Secret: fixture-secret', sent[0])
        self.assertIn('127.0.0.1:8877/event', sent[0])

    def test_panel_token_prefetches_bot_image(self):
        """Край 3: config пуст, токен в runtime узла - образ бота тянется заранее."""
        self.panel_bot()
        self.assertTrue(self.prefetched(), self.calls.read_text())

    def test_config_token_announces_and_prefetches(self):
        """Положительный контроль обвязки: токен в config - как сейчас."""
        self.config(TELEGRAM_BOT_TOKEN='123456:test-only', TELEGRAM_CHAT_ID='100')
        self.node(dict(bot_token='123456:test-only', chat_id='100', admin_username=''))
        self.assertEqual(len(self.announced()), 1, self.calls.read_text())
        self.assertTrue(self.prefetched(), self.calls.read_text())

    def test_no_token_anywhere_stays_silent(self):
        """Край 3: config пуст и runtime пуст или его нет - не шлёт, а образ бота тянет:
        стадия 35 ставит бота при любом deploy."""
        for runtime in (dict(bot_token='', chat_id='', admin_username=''), None):
            with self.subTest(runtime=runtime):
                self.setUp()
                self.config()
                self.node(runtime)
                self.assertEqual(self.announced(), [], self.calls.read_text())
                self.assertTrue(self.prefetched(), self.calls.read_text())

    def test_image_pull_off_skips_bot_prefetch(self):
        """Край 3: TGBOT_IMAGE_PULL=false - единственное, что выключает предзагрузку образа бота."""
        self.config(TELEGRAM_BOT_TOKEN='123456:test-only', TGBOT_IMAGE_PULL='false')
        self.node(dict(bot_token='123456:test-only', chat_id='100', admin_username=''))
        self.assertEqual(self.prefetched(), [], self.calls.read_text())


if __name__ == '__main__':
    unittest.main()
