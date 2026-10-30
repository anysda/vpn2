"""Бот ставится всегда: без токена в config.yaml, а токен и Chat ID приходят из панели.

Стадия 35 идёт в той же обвязке, что и test_telegram_stage (двойник docker, без root).
Отдельная стадия 35 на установленном сервере получает файлы только от deploy.sh.
"""
import json
import os
import shutil
import subprocess
import unittest

from test_deploy import ROOT, function
import test_telegram_stage as stage


RUNTIME_MOUNT = 'etc/anysda/telegram-runtime.json:ro'


class TelegramAlwaysInstalledTests(unittest.TestCase):
    setUp = stage.TelegramStageTests.setUp
    run_stage = stage.TelegramStageTests.run_stage
    data = stage.TelegramStageTests.data
    container_env = stage.TelegramStageTests.container_env

    def bot_run(self):
        runs = [call for call in self.data()['calls']
                if call[0] == 'run' and 'anysda-tgbot' in call]
        self.assertTrue(runs, 'docker run anysda-tgbot не вызывался')
        return runs[-1]

    def assert_installed(self, **env):
        result = self.run_stage(**env)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn('anysda-tgbot', self.data()['containers'], output)
        secret = (self.root / 'etc/anysda/tgbot-secret.txt').read_text()
        self.assertEqual(len(secret), 32)
        self.assertEqual(self.container_env('anysda-tgbot')['TGBOT_SECRET'], secret)
        self.assertEqual(self.container_env('anysda-vpn2')['NUXT_TGBOT_SECRET'], secret)
        # «Сохранить» в панели пишет этот файл: он должен быть смонтирован в бота.
        self.assertTrue(any(arg.endswith(RUNTIME_MOUNT) for arg in self.bot_run()), self.bot_run())
        self.assertTrue((self.root / 'var/lib/anysda-vpn2/runtime/telegram-runtime.json').is_file())
        return output

    def test_installed_without_token(self):
        self.assert_installed(TELEGRAM_BOT_TOKEN='')

    def test_installed_without_chat_id(self):
        self.assert_installed(TELEGRAM_CHAT_ID='')

    def test_installed_without_token_and_chat_id(self):
        self.assert_installed(TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID='')

    def test_redeploy_without_token_keeps_token_saved_in_panel(self):
        empty = dict(TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID='', TELEGRAM_ADMIN_USERNAME='')
        self.assert_installed(**empty)
        runtime = self.root / 'etc/anysda/telegram-runtime.json'
        saved = dict(bot_token='987654:panel-only', chat_id='200', admin_username='panel_operator')
        runtime.write_text(json.dumps(saved))
        output = self.assert_installed(**empty)
        self.assertNotIn('987654:panel-only', output)
        self.assertEqual(json.loads(runtime.read_text()), saved)

    def test_with_token_and_chat_id_same_container_and_runtime(self):
        self.assert_installed()
        env = self.container_env('anysda-tgbot')
        self.assertEqual(env['TELEGRAM_BOT_TOKEN'], '123456:test-only')
        self.assertEqual(env['TELEGRAM_CHAT_ID'], '100')
        runtime = json.loads((self.root / 'etc/anysda/telegram-runtime.json').read_text())
        self.assertEqual(runtime, dict(bot_token='123456:test-only', chat_id='100',
                                       admin_username='operator'))

    def deploy_pushes(self, stage_name):
        """Что run_stage_on_host из deploy.sh кладёт в /tmp/anysda узла."""
        deploy = self.root / 'checkout/infra'
        deploy.mkdir(parents=True)
        for name in ('scripts', 'lib', 'configs'):
            (deploy / name).symlink_to(ROOT / 'infra' / name)
        (deploy.parent / 'app').symlink_to(ROOT / 'app')
        (deploy / 'envs').mkdir()
        (deploy / 'envs/all.env').write_text('COMMON=1\n')
        (deploy / 'envs/ru.env').write_text('HOST_TAG=ru\n')
        node = self.root / 'node'
        node.mkdir()
        script = function('run_stage_on_host') + function('fw_guard_stage') + '''
DEPLOY_ROOT=$PWD
FW_GUARD_STAGES=''
load_env() { HOST_TAG=$1; }
log() { :; }
ok() { :; }
warn() { echo "warn: $*" >&2; }
die() { echo "die: $*" >&2; exit 1; }
push() { cp -R "$1" "$NODE/${2:-$(basename "$1")}"; }
ssh_exec() { :; }
run_stage_on_host "$STAGE" ru
'''
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=deploy,
                                env=dict(os.environ, NODE=str(node), STAGE=stage_name),
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return node

    def test_stage_alone_on_installed_server_gets_files_from_deploy(self):
        node = self.deploy_pushes('35-telegram')
        pushed = {path.name for path in node.iterdir()}
        scripts = self.root / 'scripts'
        # Обвязка стадий подменяет только то, что deploy действительно доставил;
        # остальные файлы приходят на узел ровно такими, какими их кладёт deploy.
        for path in scripts.iterdir():
            if path.name not in pushed:
                path.unlink()
            elif not path.name.endswith('.sh'):
                shutil.copyfile(node / path.name, path)
        # Установленный сервер: панель уже стоит, секрета бота у неё ещё нет.
        self.assertEqual(self.container_env('anysda-vpn2')['NUXT_TGBOT_SECRET'], '')
        result = self.run_stage()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        secret = (self.root / 'etc/anysda/tgbot-secret.txt').read_text()
        self.assertEqual(self.container_env('anysda-vpn2')['NUXT_TGBOT_SECRET'], secret)
        self.assertIn('anysda-tgbot', self.data()['containers'])


if __name__ == '__main__':
    unittest.main()
