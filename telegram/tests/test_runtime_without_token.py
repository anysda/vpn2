"""Бот без токена живёт и ждёт: токен и Chat ID из панели включают опрос без деплоя.

Настоящий bot.py идёт отдельным процессом, двойник политики `--restart always`
поднимает его заново после выхода и считает выходы. Сеть Telegram подменена
двойником на уровне httpx: он отвечает на любой метод и записывает, с каким
токеном его звали.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest


BOT = Path(__file__).resolve().parents[1] / 'bot.py'
FIRST = '123456:TEST_ONLY_FIRST'
SECOND = '654321:TEST_ONLY_SECOND'
# Окно, за которое бот обязан подхватить telegram-runtime.json (карточка - до 60 с).
PICKUP_SEC = 30
# Пауза перед подъёмом после выхода, как у docker --restart always.
RESTART_DELAY_SEC = 0.5

LAUNCHER = r'''
import asyncio, json, re, sys
from pathlib import Path
import httpx

bot_path, runtime, calls = sys.argv[1:4]
sys.path.insert(0, str(Path(bot_path).parent))
real_send = httpx.AsyncClient.send


async def send(self, request, **kwargs):
    if request.url.host != 'api.telegram.org':
        return await real_send(self, request, **kwargs)
    match = re.fullmatch(r'/bot([^/]*)/(\w+)', request.url.path)
    token, method = match.groups() if match else ('', request.url.path)
    with open(calls, 'a') as log:
        log.write(json.dumps([token, method]) + '\n')
    if method == 'getUpdates':
        await asyncio.sleep(0.2)
        result = []
    elif method == 'getMe':
        result = {'id': 1, 'is_bot': True, 'first_name': 'Test', 'username': 'test_bot'}
    else:
        result = True
    return httpx.Response(200, json={'ok': True, 'result': result}, request=request)


httpx.AsyncClient.send = send
source = Path(bot_path).read_text().replace('/etc/anysda/telegram-runtime.json', runtime)
exec(compile(source, bot_path, 'exec'), {'__name__': '__main__', '__file__': bot_path})
'''


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class RuntimeWithoutTokenTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='tgbot-runtime-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.runtime = self.root / 'telegram-runtime.json'
        self.calls = self.root / 'calls.jsonl'
        self.output = self.root / 'bot.log'
        self.exits = 0
        self.stopping = threading.Event()
        self.process = None

    def save(self, token, chat_id):
        """Так «Сохранить» в панели пишет telegram-runtime.json."""
        self.runtime.write_text(json.dumps(
            {'bot_token': token, 'chat_id': chat_id, 'admin_username': ''}))

    def start(self):
        # Окружение как у контейнера, поставленного без токена в config.yaml.
        env = dict(os.environ, TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID='',
                   TELEGRAM_ADMIN_USERNAME='', TGBOT_SECRET='evt-secret',
                   TGBOT_EVENT_PORT=str(free_port()), ANYSDA_URL='http://127.0.0.1:9',
                   TG_PROXY='http://127.0.0.1:9', TGBOT_INVALID_TOKEN_PAUSE_SEC='0',
                   TGBOT_OUTBOX_PATH=str(self.root / 'outbox.sqlite'))
        output = self.output.open('a')
        self.addCleanup(output.close)

        def restart_always():
            while not self.stopping.is_set():
                self.process = subprocess.Popen(
                    [sys.executable, '-c', LAUNCHER, str(BOT), str(self.runtime), str(self.calls)],
                    env=env, stdout=output, stderr=subprocess.STDOUT)
                self.process.wait()
                if self.stopping.is_set():
                    return
                self.exits += 1
                self.stopping.wait(RESTART_DELAY_SEC)

        supervisor = threading.Thread(target=restart_always, daemon=True)
        supervisor.start()
        self.addCleanup(self.stop, supervisor)

    def stop(self, supervisor):
        self.stopping.set()
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
        supervisor.join(timeout=15)

    def log(self):
        return self.output.read_text()[-3000:]

    def polls(self, token=None):
        if not self.calls.exists():
            return 0
        lines = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return sum(1 for used, method in lines
                   if method == 'getUpdates' and token in (None, used))

    def assert_no_exits(self, since, what):
        self.assertEqual(self.exits - since, 0,
                         f'{what}: бот завершался {self.exits - since} раз (круг перезапусков):\n'
                         + self.log())

    def wait_polls(self, token):
        deadline = time.monotonic() + PICKUP_SEC
        while time.monotonic() < deadline:
            if self.polls(token):
                return
            time.sleep(0.2)
        self.fail(f'нет getUpdates с токеном {token} за {PICKUP_SEC} с, выходов {self.exits}:\n'
                  + self.log())

    def test_without_token_stays_up_and_does_not_poll(self):
        self.save('', '')
        self.start()
        time.sleep(10)
        self.assert_no_exits(0, 'без токена, файл не меняли')
        self.assertEqual(self.polls(), 0)

    def test_token_and_chat_id_from_panel_start_polling(self):
        self.save('', '')
        self.start()
        time.sleep(5)
        self.assert_no_exits(0, 'без токена до сохранения в панели')
        self.save(FIRST, '100')
        self.wait_polls(FIRST)
        self.assertLessEqual(self.exits, 1, self.log())

    def test_changed_token_polls_with_new_token_only(self):
        self.save(FIRST, '100')
        self.start()
        self.wait_polls(FIRST)
        before = self.exits
        self.save(SECOND, '100')
        self.wait_polls(SECOND)
        time.sleep(1)
        first = self.polls(FIRST)
        time.sleep(5)
        self.assertEqual(self.polls(FIRST), first, 'старый токен продолжает опрос')
        self.assertLessEqual(self.exits - before, 1, self.log())

    def test_removed_token_stops_polling_without_restart_loop(self):
        self.save(FIRST, '100')
        self.start()
        self.wait_polls(FIRST)
        before = self.exits
        self.save('', '')
        time.sleep(PICKUP_SEC / 2)
        self.assertLessEqual(self.exits - before, 1, 'после стирания токена больше одного выхода:\n'
                             + self.log())
        settled, polls = self.exits, self.polls()
        time.sleep(10)
        self.assertEqual(self.polls(), polls, 'опрос не прекратился')
        self.assert_no_exits(settled, 'после стирания токена')


if __name__ == '__main__':
    unittest.main()
