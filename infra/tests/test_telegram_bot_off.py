"""Токен из config.yaml выключается деплоем без него, токен из панели деплой не трогает.

Стадия 35 идёт в обвязке test_telegram_stage (двойник docker, без root).
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
import test_telegram_stage as stage


EMPTY = dict(TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID='', TELEGRAM_ADMIN_USERNAME='')


class TelegramConfigRemovedTests(unittest.TestCase):
    setUp = stage.TelegramStageTests.setUp
    run_stage = stage.TelegramStageTests.run_stage
    ok = stage.TelegramStageTests.ok
    data = stage.TelegramStageTests.data
    container_env = stage.TelegramStageTests.container_env

    def runtime(self):
        return json.loads((self.root / 'etc/anysda/telegram-runtime.json').read_text())

    def save_in_panel(self, **fields):
        path = self.root / 'etc/anysda/telegram-runtime.json'
        saved = self.runtime() | fields
        path.write_text(json.dumps(saved))
        return saved

    def assert_bot_waits(self):
        secret = (self.root / 'etc/anysda/tgbot-secret.txt').read_text()
        self.assertEqual(len(secret), 32)
        self.assertIn('anysda-tgbot', self.data()['containers'])
        self.assertEqual(self.container_env('anysda-tgbot')['TGBOT_SECRET'], secret)
        self.assertEqual(self.container_env('anysda-vpn2')['NUXT_TGBOT_SECRET'], secret)

    def test_config_token_removed_from_config_disables_bot(self):
        """Край 1: токен и Chat ID из config убрали - deploy выключает бота."""
        self.ok()
        self.ok()
        self.assertEqual(self.runtime()['bot_token'], '123456:test-only')
        for _ in range(2):
            self.ok(**EMPTY)
            runtime = self.runtime()
            self.assertEqual(runtime.get('bot_token', ''), '', runtime)
            self.assertEqual(runtime.get('chat_id', ''), '', runtime)
            self.assert_bot_waits()
            self.assertEqual(self.container_env('anysda-tgbot')['TELEGRAM_BOT_TOKEN'], '')

    def test_panel_token_after_config_removed_survives_deploy(self):
        """Край 2: config убрали, бота включили из панели - deploy без токена его не стирает."""
        self.ok()
        self.ok(**EMPTY)
        saved = self.save_in_panel(bot_token='987654:panel-only', chat_id='200')
        for _ in range(2):
            self.ok(**EMPTY)
            self.assertEqual(self.runtime(), saved)
            self.assert_bot_waits()

    def test_config_token_replaced_in_panel_survives_deploy_without_config(self):
        """Край 2: токен из config заменили в панели - deploy без telegram оставляет токен панели."""
        self.ok()
        saved = self.save_in_panel(bot_token='987654:panel-only', chat_id='200')
        self.ok()
        self.assertEqual(self.runtime(), saved)
        for _ in range(2):
            self.ok(**EMPTY)
            runtime = self.runtime()
            self.assertEqual(runtime['bot_token'], '987654:panel-only', runtime)
            self.assertEqual(runtime['chat_id'], '200', runtime)
            self.assert_bot_waits()

    def remove_bot_container(self):
        """Контейнер бота удалён руками или стадия 35 упала между docker rm и docker run."""
        data = self.data()
        data['containers'].pop('anysda-tgbot')
        self.state.write_text(json.dumps(data))

    def test_config_token_removed_without_bot_container(self):
        """Край 1: контейнера бота нет - deploy без telegram всё равно выключает бота из config."""
        self.ok()
        self.assertEqual(self.runtime()['bot_token'], '123456:test-only')
        self.remove_bot_container()
        for _ in range(2):
            self.ok(**EMPTY)
            runtime = self.runtime()
            self.assertEqual(runtime.get('bot_token', ''), '', runtime)
            self.assertEqual(runtime.get('chat_id', ''), '', runtime)
            self.assert_bot_waits()
            self.assertEqual(self.container_env('anysda-tgbot')['TELEGRAM_BOT_TOKEN'], '')

    def test_panel_token_survives_deploy_without_bot_container(self):
        """Край 2: контейнера бота нет, токен заменён в панели - deploy без telegram его оставляет."""
        self.ok()
        saved = self.save_in_panel(bot_token='987654:panel-only', chat_id='200')
        self.remove_bot_container()
        for _ in range(2):
            self.ok(**EMPTY)
            self.assertEqual(self.runtime(), saved)
            self.assert_bot_waits()

    def test_new_config_token_wins_over_runtime(self):
        """Край 4: новый токен в config побеждает токен в runtime, бот включается снова."""
        self.ok()
        self.save_in_panel(bot_token='987654:panel-only')
        self.ok(TELEGRAM_BOT_TOKEN='765432:new-token')
        self.assertEqual(self.runtime()['bot_token'], '765432:new-token')
        self.ok(**EMPTY)
        self.ok(TELEGRAM_BOT_TOKEN='765432:new-token')
        self.assertEqual(self.runtime()['bot_token'], '765432:new-token')
        self.assertEqual(self.runtime()['chat_id'], '100')


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
