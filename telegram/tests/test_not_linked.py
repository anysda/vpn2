import json
import unittest
from unittest.mock import patch

import httpx

from support import load_bot, update

NOT_LINKED = {'error': True, 'statusCode': 404, 'statusMessage': 'not_linked',
              'message': 'not_linked'}
VIEW = {'id': 1, 'name': 'client', 'deviceLimit': 3,
        'devices': [{'id': 7, 'name': 'phone', 'configName': 'client-phone',
                     'hasWg': True, 'hasOvpn': True}]}


class Telegram:
    """Bot API: второй ответ на ту же кнопку отклоняется, как у настоящего Telegram."""

    def __init__(self):
        self.calls = []
        self.answered = set()

    def __call__(self, request):
        method = request.url.path.rsplit('/', 1)[-1]
        body = json.loads(request.content) if request.content else {}
        self.calls.append((method, body))
        if method == 'answerCallbackQuery':
            if body['callback_query_id'] in self.answered:
                return httpx.Response(400, json={
                    'ok': False, 'error_code': 400,
                    'description': 'Bad Request: query is too old and response timeout '
                                   'expired or query ID is invalid'})
            self.answered.add(body['callback_query_id'])
        return httpx.Response(200, json={'ok': True, 'result': {}})

    def sent(self, chat_id):
        return [b['text'] for m, b in self.calls
                if m == 'sendMessage' and b.get('chat_id') == chat_id]

    def alerts(self):
        return [b.get('text') for m, b in self.calls if m == 'answerCallbackQuery']


class NotLinkedReplyTests(unittest.IsolatedAsyncioTestCase):
    """Отвязанный чат получает одно и то же «нет доступа», где бы ни узнал об отвязке."""

    async def asyncSetUp(self):  # noqa - unittest lifecycle hook, called by the framework
        self.bot = load_bot({'TELEGRAM_ADMIN_USERNAME': 'vpn_admin'})
        self.addAsyncCleanup(self.bot._local.aclose)
        self.addAsyncCleanup(self.bot._tg.aclose)
        self.tg = Telegram()
        tg = httpx.AsyncClient(base_url='http://telegram.test', transport=httpx.MockTransport(self.tg))
        self.addAsyncCleanup(tg.aclose)
        patcher = patch.object(self.bot, '_tg', tg)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _with_panel(self, unlinked_path, action):
        """Панель отвечает not_linked только на unlinked_path (отвязка посреди нажатия)."""
        def panel(request):
            if unlinked_path is None or request.url.path.endswith(unlinked_path):
                return httpx.Response(404, json=NOT_LINKED)
            if request.url.path == '/api/bot/client':
                return httpx.Response(200, json=VIEW)
            return httpx.Response(200, text='[Interface]\n')

        async with httpx.AsyncClient(base_url='http://panel.test',
                                     transport=httpx.MockTransport(panel)) as client:
            with patch.object(self.bot, '_local', client):
                await action()

    async def _expected_text(self):
        """Ответ обычному сообщению из отвязанного чата - эталон."""
        await self._with_panel(None, lambda: self.bot.on_text(update(text='привет'), None))
        reference = self.tg.sent(200)
        self.assertEqual(reference, [self.bot._no_access_text()])
        self.tg.calls.clear()
        return reference[0]

    async def test_config_unlinked_mid_request_gets_no_access_text(self):
        expected = await self._expected_text()
        for action, path in (('wg', '/wg-config'), ('ovpn', '/ovpn-config')):
            with self.subTest(action=action):
                self.tg.calls.clear()
                self.tg.answered.clear()
                await self._with_panel(path, lambda: self.bot.on_callback(
                    update(data=f'{action}:7'), None))
                self.assertEqual(self.tg.sent(200), [expected])
                self.assertNotIn('not_linked', json.dumps(self.tg.calls, ensure_ascii=False))

    async def test_every_client_button_from_unlinked_chat_gets_no_access_text(self):
        expected = await self._expected_text()
        for data in ('devs', 'dev:7', 'wg:7', 'ovpn:7', 'del:7', 'reicyes:7', 'delyes:7'):
            with self.subTest(data=data):
                self.tg.calls.clear()
                self.tg.answered.clear()
                await self._with_panel('', lambda: self.bot.on_callback(update(data=data), None))
                self.assertEqual(self.tg.sent(200), [expected])
                self.assertNotIn('not_linked', json.dumps(self.tg.calls, ensure_ascii=False))

    async def test_add_device_unlinked_mid_request_gets_no_access_text(self):
        expected = await self._expected_text()
        self.bot._awaiting_device_name.add(200)
        await self._with_panel('/api/bot/devices', lambda: self.bot.on_text(
            update(text='laptop'), None))
        self.assertEqual(self.tg.sent(200), [expected])

    async def test_error_after_answered_button_reaches_chat(self):
        def panel(request):
            if request.url.path == '/api/bot/client':
                return httpx.Response(200, json=VIEW)
            return httpx.Response(503, json={'statusMessage': 'wg_disabled',
                                             'message': 'WireGuard выключен'})

        async with httpx.AsyncClient(base_url='http://panel.test',
                                     transport=httpx.MockTransport(panel)) as client:
            with patch.object(self.bot, '_local', client):
                await self.bot.on_callback(update(data='wg:7'), None)
        self.assertEqual(self.tg.sent(200), ['❌ Панель временно недоступна. Попробуйте позже.'])
        self.assertEqual(self.tg.alerts()[0], 'Готовлю конфиг WireGuard…')


if __name__ == '__main__':
    unittest.main()
