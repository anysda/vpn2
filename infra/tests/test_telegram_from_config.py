"""Файл «взятое из config» (telegram-from-config.json): битый не валит стадию 35,
запись не оставляет пустого файла.

Стадия 35 идёт в обвязке test_telegram_stage (двойник docker, без root).
"""
import json
import os
import unittest

import test_telegram_stage as stage


EMPTY = dict(TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID='', TELEGRAM_ADMIN_USERNAME='')
CONFIG = '123456:test-only'
PANEL = '987654:panel-only'
BROKEN = ('', '{bro', 'null', '[]')

# Обрыв записи: процесс гибнет в момент, когда трогает сам файл. Открыл его на
# запись - файл к этому мгновению усечён (O_TRUNC), rename на него - ещё не сделан.
INTERRUPT = '''import os, sys
TARGET = os.environ['FROM_CONFIG_PATH']
def same(path):
    try:
        return os.path.abspath(os.fsdecode(path)) == TARGET
    except TypeError:
        return False
def hook(event, args):
    if event == 'open' and same(args[0]):
        mode, flags = args[1], args[2]
        if isinstance(mode, str):
            writing = any(c in mode for c in 'wax+')
        else:
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR))
        if writing:
            os.truncate(TARGET, 0)
            os._exit(137)
    elif event == 'os.rename' and same(args[1]):
        os._exit(137)
sys.addaudithook(hook)
'''


class TelegramFromConfigTests(unittest.TestCase):
    setUp = stage.TelegramStageTests.setUp
    run_stage = stage.TelegramStageTests.run_stage
    ok = stage.TelegramStageTests.ok
    data = stage.TelegramStageTests.data
    container_env = stage.TelegramStageTests.container_env

    @property
    def taken(self):
        return self.root / 'etc/anysda/telegram-from-config.json'

    def runtime(self):
        return json.loads((self.root / 'etc/anysda/telegram-runtime.json').read_text())

    def save_in_panel(self, **fields):
        path = self.root / 'etc/anysda/telegram-runtime.json'
        saved = self.runtime() | fields
        path.write_text(json.dumps(saved))
        return saved

    def remove_bot_container(self):
        data = self.data()
        data['containers'].pop('anysda-tgbot')
        self.state.write_text(json.dumps(data))

    def assert_taken_written(self, **expected):
        """После стадии файл - целый JSON с тем, что стадия взяла из config, 0600."""
        self.assertEqual(json.loads(self.taken.read_text()),
                         dict(bot_token='', chat_id='', admin_username='') | expected)
        self.assertEqual(os.stat(self.taken).st_mode & 0o777, 0o600)

    def test_broken_file_falls_back_to_bot_container_env(self):
        """Край 1: файл битый, контейнер бота с токеном config есть - стадия идёт,
        прошлое берётся из env контейнера, токен config выключается."""
        for body in BROKEN:
            with self.subTest(body=body):
                self.setUp()
                self.ok()
                self.assertEqual(self.runtime()['bot_token'], CONFIG)
                self.taken.write_text(body)
                self.ok(**EMPTY)
                runtime = self.runtime()
                self.assertEqual(runtime['bot_token'], '', runtime)
                self.assertEqual(runtime['chat_id'], '', runtime)
                self.assert_taken_written()

    def test_broken_file_without_bot_container_keeps_token(self):
        """Край 1: файл битый и контейнера бота нет - прошлого не знаем, токен остаётся."""
        for body in BROKEN:
            with self.subTest(body=body):
                self.setUp()
                self.ok()
                self.remove_bot_container()
                self.taken.write_text(body)
                self.ok(**EMPTY)
                runtime = self.runtime()
                self.assertEqual(runtime['bot_token'], CONFIG, runtime)
                self.assertEqual(runtime['chat_id'], '100', runtime)
                self.assert_taken_written()

    def test_broken_file_keeps_panel_token(self):
        """Край 1: файл битый, токен заменён в панели - deploy без telegram его не трогает."""
        for body in BROKEN:
            with self.subTest(body=body):
                self.setUp()
                self.ok()
                saved = self.save_in_panel(bot_token=PANEL, chat_id='200')
                self.taken.write_text(body)
                for _ in range(2):
                    self.ok(**EMPTY)
                    self.assertEqual(self.runtime(), saved)
                self.assert_taken_written()

    def test_interrupted_write_keeps_previous_file(self):
        """Край 1: запись файла оборвана - на месте прежний целый файл или новый целый."""
        self.ok()
        before = json.loads(self.taken.read_text())
        hooks = self.root / 'interrupt'
        hooks.mkdir()
        (hooks / 'sitecustomize.py').write_text(INTERRUPT)
        result = self.run_stage(**EMPTY, PYTHONPATH=str(hooks),
                                FROM_CONFIG_PATH=os.path.abspath(self.taken))
        after = self.taken.read_text()
        self.assertTrue(after, f'запись оставила пустой файл, rc={result.returncode}')
        self.assertIn(json.loads(after), (before, dict(bot_token='', chat_id='', admin_username='')))

if __name__ == '__main__':
    unittest.main()
