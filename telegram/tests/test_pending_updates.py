"""Опрос Telegram: накопленное за перезапуск получает ответ, обрыв связи - одна строка в журнале.

Настоящий пакет vpn2bot идёт отдельным процессом; Bot API подменён на уровне httpx
и, как Telegram, держит накопленные обновления, пока их не сбросят
deleteWebhook(drop_pending_updates=true) или не подтвердят offset.
"""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from telegram.error import TimedOut

from harness import import_bot
import ports

BOT = Path(__file__).resolve().parents[1] / 'vpn2bot'
CHAT = 55
LAUNCHER = r'''
import asyncio, json, re, sys, urllib.parse
from pathlib import Path
import httpx

bot_path, sent = sys.argv[1:3]
broken = []
sys.path.insert(0, str(Path(bot_path).parent))
real_send = httpx.AsyncClient.send
pending = [{'update_id': 7, 'message': {'message_id': 3, 'date': 0, 'text': '/start',
            'entities': [{'type': 'bot_command', 'offset': 0, 'length': 6}],
            'chat': {'id': %d, 'type': 'private'}, 'from': {'id': %d, 'is_bot': False, 'first_name': 'x'}}}]


async def send(self, request, **kwargs):
    if request.url.host != 'api.telegram.org':
        return await real_send(self, request, **kwargs)
    method = request.url.path.rsplit('/', 1)[-1]
    form = {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()}
    if method == 'deleteWebhook' and form.get('drop_pending_updates') == 'true':
        pending.clear()
    if method == 'getUpdates':
        if not broken:
            broken.append(1)
            raise httpx.RemoteProtocolError('Server disconnected without sending a response.')
        pending[:] = [u for u in pending if u['update_id'] >= int(form.get('offset', 0))]
        result = list(pending)
        if not result:
            await asyncio.sleep(0.2)
    elif method == 'getMe':
        result = {'id': 1, 'is_bot': True, 'first_name': 'Test', 'username': 'test_bot'}
    elif method == 'sendMessage':
        with open(sent, 'a') as log:
            log.write(form.get('chat_id', '') + '\n')
        result = {'message_id': 9, 'date': 0, 'chat': {'id': int(form['chat_id']), 'type': 'private'}}
    else:
        result = True
    return httpx.Response(200, json={'ok': True, 'result': result}, request=request)


httpx.AsyncClient.send = send
import runpy
runpy.run_module('vpn2bot', run_name='__main__', alter_sys=True)
''' % (CHAT, CHAT)


class PendingUpdatesTests(unittest.TestCase):
    def test_pending_message_answered_and_dropped_link_is_one_line(self):
        directory = tempfile.TemporaryDirectory(prefix='tgbot-pending-')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        sent, output = root / 'sent.txt', (root / 'bot.log').open('w')
        self.addCleanup(output.close)
        env = dict(os.environ, TELEGRAM_BOT_TOKEN='123456:TEST_ONLY', TELEGRAM_CHAT_ID=str(CHAT),
                   TGBOT_SECRET='evt-secret', TGBOT_EVENT_PORT=str(ports.event_port(self)),
                   ANYSDA_URL='http://127.0.0.1:9', TG_PROXY='http://127.0.0.1:9',
                   TGBOT_OUTBOX_PATH=str(root / 'outbox.sqlite'))
        bot = subprocess.Popen([sys.executable, '-c', LAUNCHER, str(BOT), str(sent)],
                               env=env, stdout=output, stderr=subprocess.STDOUT)
        self.addCleanup(bot.wait)
        self.addCleanup(bot.kill)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (sent.exists() and sent.read_text()):
            time.sleep(0.2)
        self.assertIn(str(CHAT), sent.read_text().split() if sent.exists() else [],
                      (root / 'bot.log').read_text()[-3000:])
        log = (root / 'bot.log').read_text()
        self.assertNotIn('Traceback', log)
        self.assertEqual(len([line for line in log.splitlines() if 'нет связи с Telegram' in line]), 1, log[-3000:])

    def test_other_errors_keep_traceback(self):
        bot = import_bot()
        self.addCleanup(lambda: asyncio.run(bot.api.panel_http.aclose()))
        try:
            raise RuntimeError('сломалось')
        except RuntimeError as e:
            error = e
        with self.assertLogs('tgbot', 'WARNING') as logs:
            asyncio.run(sys.modules['vpn2bot.__main__'].log_error(None, SimpleNamespace(error=error)))
            asyncio.run(sys.modules['vpn2bot.__main__'].log_error(None, SimpleNamespace(error=TimedOut())))
        self.assertEqual([r.levelname for r in logs.records], ['ERROR', 'WARNING'])
        self.assertIs(logs.records[0].exc_info[1], error)
        self.assertIsNone(logs.records[1].exc_info)
