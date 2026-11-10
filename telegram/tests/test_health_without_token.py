"""HEALTHCHECK контейнера: бот без токена здоров, бот с зависшим getUpdates - нет.

Настоящий пакет vpn2bot идёт отдельным процессом, сеть Telegram подменена двойником на
уровне httpx. Проверку здоровья делает та же команда, что HEALTHCHECK в
telegram/Dockerfile: её и start-period читаем из файла.
"""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request


TELEGRAM = Path(__file__).resolve().parents[1]
BOT = TELEGRAM / 'vpn2bot'
DOCKERFILE = TELEGRAM / 'Dockerfile'
TOKEN = '123456:TEST_ONLY_TOKEN'
STALL_SEC = 3

LAUNCHER = r'''
import asyncio, re, sys
from pathlib import Path
import httpx

bot_path, runtime = sys.argv[1:3]
sys.path.insert(0, str(Path(bot_path).parent))
real_send = httpx.AsyncClient.send


async def send(self, request, **kwargs):
    if request.url.host != 'api.telegram.org':
        return await real_send(self, request, **kwargs)
    if 'rejected' in request.url.path:
        return httpx.Response(401, json={'ok': False, 'error_code': 401,
                                         'description': 'Unauthorized'}, request=request)
    method = request.url.path.rsplit('/', 1)[-1]
    if method == 'getUpdates':
        await asyncio.sleep(3600)
    if method == 'getMe':
        result = {'id': 1, 'is_bot': True, 'first_name': 'Test', 'username': 'test_bot'}
    else:
        result = True
    return httpx.Response(200, json={'ok': True, 'result': result}, request=request)


httpx.AsyncClient.send = send
import importlib.util
import runpy
# Путь к runtime-файлу подменяем в исходнике config; чужой байт-код не используется.
spec = importlib.util.find_spec('vpn2bot.config')
source = Path(spec.origin).read_text()
assert '/etc/anysda/telegram-runtime.json' in source, 'путь runtime не найден в config.py'
source = source.replace('/etc/anysda/telegram-runtime.json', runtime)
module = importlib.util.module_from_spec(spec)
sys.modules['vpn2bot.config'] = module
exec(compile(source, spec.origin, 'exec'), module.__dict__)
runpy.run_module('vpn2bot', run_name='__main__', alter_sys=True)
'''


def healthcheck():
    """Команда и start-period из HEALTHCHECK в Dockerfile."""
    text = DOCKERFILE.read_text().replace('\\\n', ' ')
    match = re.search(r'^HEALTHCHECK\s+(.*?)\s+CMD\s+(.+)$', text, re.M)
    options, command = match.groups()
    start = re.search(r'--start-period=(\d+)s', options)
    return command.strip(), int(start.group(1))


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class HealthWithoutTokenTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='tgbot-health-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / 'bot.log'
        self.command, self.start_period = healthcheck()
        # В образе `python` - интерпретатор контейнера; здесь - тот, что гоняет тесты.
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'python').symlink_to(sys.executable)
        self.port = free_port()
        self.env = dict(os.environ, PATH=f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}',
                        TGBOT_EVENT_PORT=str(self.port), TELEGRAM_ADMIN_USERNAME='',
                        TGBOT_SECRET='evt-secret', ANYSDA_URL='http://127.0.0.1:9',
                        TG_PROXY='http://127.0.0.1:9', TGBOT_INVALID_TOKEN_PAUSE_SEC='0',
                        TGBOT_POLL_STALL_SEC=str(STALL_SEC),
                        TGBOT_OUTBOX_PATH=str(self.root / 'outbox.sqlite'))

    def start(self, token, chat_id, env=None):
        runtime = self.root / 'telegram-runtime.json'
        runtime.write_text(json.dumps(
            {'bot_token': token, 'chat_id': chat_id, 'admin_username': ''}))
        env = dict(env or self.env, TELEGRAM_BOT_TOKEN=token, TELEGRAM_CHAT_ID=chat_id)
        output = self.output.open('a')
        self.addCleanup(output.close)
        process = subprocess.Popen(
            [sys.executable, '-c', LAUNCHER, str(BOT), str(runtime)],
            env=env, stdout=output, stderr=subprocess.STDOUT)
        self.addCleanup(process.wait, 15)
        self.addCleanup(process.kill)
        return process

    def check(self):
        return subprocess.run(['sh', '-c', self.command], env=self.env, capture_output=True,
                              text=True, timeout=30)

    def log(self):
        return self.output.read_text()[-3000:]

    def test_without_token_is_healthy_within_start_period(self):
        process = self.start('', '')
        deadline = time.monotonic() + self.start_period
        while True:
            result = self.check()
            if result.returncode == 0:
                break
            if time.monotonic() > deadline:
                self.fail(f'HEALTHCHECK не прошёл за start-period {self.start_period} с, '
                          f'код {result.returncode}:\n{result.stderr[-1500:]}\nлог бота:\n'
                          + self.log())
            time.sleep(1)
        self.assertIsNone(process.poll(), 'бот без токена завершился:\n' + self.log())

    def test_token_with_stalled_get_updates_is_unhealthy(self):
        process = self.start(TOKEN, '100')
        time.sleep(STALL_SEC + 4)
        self.assertIsNone(process.poll(), 'бот завершился раньше проверки:\n' + self.log())
        result = self.check()
        self.assertNotEqual(result.returncode, 0, 'HEALTHCHECK прошёл при зависшем getUpdates')
        # Красное от /health (503), а не от закрытого порта.
        self.assertIn('503', result.stderr, result.stderr[-1500:] + '\nлог бота:\n' + self.log())

    def test_rejected_token_pause_keeps_health_and_exits(self):
        """Токен отвергнут: на паузе /health жив, новый токен обрывает паузу, порт свободен."""
        env = dict(self.env, TGBOT_INVALID_TOKEN_PAUSE_SEC='60')
        process = self.start('1:rejected', '42', env)
        deadline = time.monotonic() + 20
        while 'InvalidToken' not in self.log():
            self.assertIsNone(process.poll(), 'бот вышел до паузы:\n' + self.log())
            if time.monotonic() > deadline:
                self.fail('в журнале нет InvalidToken за 20 с:\n' + self.log())
            time.sleep(0.5)
        self.assertIsNone(process.poll(), 'бот не на паузе, а завершился:\n' + self.log())
        result = self.check()
        self.assertEqual(result.returncode, 0, 'HEALTHCHECK красный на паузе после отвергнутого '
                         f'токена, код {result.returncode}:\n{result.stderr[-1500:]}\nлог бота:\n'
                         + self.log())
        url = f'http://127.0.0.1:{self.port}/health'
        with urllib.request.urlopen(url, timeout=5) as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
        self.assertIs(body.get('ok'), True, body)
        self.assertIs(body.get('invalid_token'), True, body)
        self.assertIsNone(process.poll(), 'бот завершился на паузе:\n' + self.log())
        runtime = self.root / 'telegram-runtime.json'
        runtime.write_text(json.dumps(
            {'bot_token': '2:new', 'chat_id': '42', 'admin_username': ''}))
        later = time.time() + 1
        os.utime(runtime, (later, later))
        try:
            code = process.wait(15)
        except subprocess.TimeoutExpired:
            self.fail('новый токен не оборвал паузу за 15 с:\n' + self.log())
        self.assertEqual(code, 1, self.log())
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(('127.0.0.1', self.port), 1)


if __name__ == '__main__':
    unittest.main()
