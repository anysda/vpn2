import logging
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from harness import import_bot, update


class ErrorPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_exception_does_not_reveal_token(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.api.panel_http.aclose)
        self.addAsyncCleanup(bot.api.telegram_http.aclose)
        with patch.object(bot.api, 'panel', AsyncMock(side_effect=RuntimeError(
                f'https://api.telegram.org/bot{bot.config.TOKEN}/sendPhoto'))), \
                patch.object(bot.api, 'answer', AsyncMock(return_value={'ok': True})) as reply:
            await bot.handlers.dispatch(update('client', ('press', 'wg:7')), None)
            self.assertNotIn(bot.config.TOKEN, str(reply.await_args))

    async def test_unexpected_panel_exception_does_not_reveal_password(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.api.panel_http.aclose)
        self.addAsyncCleanup(bot.api.telegram_http.aclose)
        password = 'TEST_ONLY_CLIENT_PASSWORD'
        with patch.object(bot.api, 'panel', AsyncMock(side_effect=RuntimeError(password))), \
                patch.object(bot.api, 'send', AsyncMock()) as reply, \
                self.assertLogs('tgbot', 'ERROR') as logs:
            await bot.handlers.dispatch(update('client', f'/start {password}'), None)
            reply.assert_awaited()
            self.assertNotIn(password, str(reply.await_args))
            self.assertNotIn(password, '\n'.join(logs.output))

    async def test_panel_server_error_body_is_not_forwarded(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.api.panel_http.aclose)
        self.addAsyncCleanup(bot.api.telegram_http.aclose)
        for response in (httpx.Response(500, json={'message': 'TEST_ONLY_PASSWORD'}),
                         httpx.Response(502, text='TEST_ONLY_PASSWORD'),
                         httpx.Response(400, text='<html>TEST_ONLY_PASSWORD</html>')):
            with self.subTest(status=response.status_code):
                self.assertNotIn('TEST_ONLY_PASSWORD', bot.api._panel_error(response).message)
        self.assertEqual(bot.api._panel_error(httpx.Response(
            409, json={'message': 'Клиент удаляется'})).message, 'Клиент удаляется')

    async def test_log_formatter_redacts_event_secret_and_proxy_credentials(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.api.panel_http.aclose)
        self.addAsyncCleanup(bot.api.telegram_http.aclose)
        record = logging.LogRecord('test', logging.ERROR, '', 1,
                                   f'{bot.config.TOKEN} {bot.config.SECRET} http://user:proxy-password@proxy.test:80',
                                   (), None)
        text = bot.config._RedactTokenFormatter().format(record)
        for secret in (bot.config.TOKEN, bot.config.SECRET, 'proxy-password'):
            self.assertNotIn(secret, text)


if __name__ == '__main__':
    unittest.main()
