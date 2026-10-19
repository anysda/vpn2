import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from support import load_bot, update


class ErrorPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_exception_does_not_reveal_token(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        with patch.object(bot, '_handle_callback', AsyncMock(side_effect=RuntimeError(
                f'https://api.telegram.org/bot{bot.TOKEN}/sendPhoto'))), \
                patch.object(bot, 'tg_answer_callback', AsyncMock()) as reply:
            await bot.on_callback(update(data='wg:7'), None)
            self.assertNotIn(bot.TOKEN, str(reply.await_args))

    async def test_unexpected_panel_exception_does_not_reveal_password(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        password = 'TEST_ONLY_CLIENT_PASSWORD'
        with patch.object(bot, 'api_link', AsyncMock(side_effect=RuntimeError(password))), \
                patch.object(bot, 'tg_send', AsyncMock()) as reply, \
                self.assertLogs('tgbot', 'ERROR') as logs:
            await bot.cmd_start(update(), SimpleNamespace(args=[password]))
            self.assertNotIn(password, str(reply.await_args))
            self.assertNotIn(password, '\n'.join(logs.output))

    async def test_panel_server_error_body_is_not_forwarded(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        for response in (httpx.Response(500, json={'message': 'TEST_ONLY_PASSWORD'}),
                         httpx.Response(502, text='TEST_ONLY_PASSWORD'),
                         httpx.Response(400, text='<html>TEST_ONLY_PASSWORD</html>')):
            with self.subTest(status=response.status_code):
                self.assertNotIn('TEST_ONLY_PASSWORD', bot._extract_error(response).message)
        self.assertEqual(bot._extract_error(httpx.Response(
            409, json={'message': 'Клиент удаляется'})).message, 'Клиент удаляется')

    async def test_log_formatter_redacts_event_secret_and_proxy_credentials(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        record = logging.LogRecord('test', logging.ERROR, '', 1,
                                   f'{bot.TOKEN} {bot.SECRET} http://user:proxy-password@proxy.test:80',
                                   (), None)
        text = bot._RedactTokenFormatter().format(record)
        for secret in (bot.TOKEN, bot.SECRET, 'proxy-password'):
            self.assertNotIn(secret, text)


if __name__ == '__main__':
    unittest.main()
