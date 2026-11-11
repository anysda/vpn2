import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from harness import import_bot


class PollHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_poll_cannot_keep_health_green(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.api.panel_http.aclose)
        self.addAsyncCleanup(bot.api.telegram_http.aclose)
        request = bot.api.WatchedRequest(proxy=bot.config.TG_PROXY)
        self.addAsyncCleanup(request.shutdown)
        responses = [(502, b'upstream down'), (409, b'{"ok":false}'),
                     (200, b'{"ok":false}'), (200, b'invalid json'),
                     (200, b'[]'), (200, b'{"ok":true}')]
        for response in responses:
            with self.subTest(response=response), patch.object(
                    bot.api.HTTPXRequest, 'do_request', AsyncMock(return_value=response)):
                # monotonic() считает от загрузки машины: 0 на свежем раннере
                # ещё не «давно», поэтому отметка заведомо старше лимита.
                stale = time.monotonic() - bot.config.POLL_STALL_SEC - 10
                bot.api.LAST_POLL_OK = stale
                await request.do_request(url='https://api.telegram.org/test', method='POST')
                self.assertEqual(bot.api.LAST_POLL_OK, stale)
                health = await bot.events.handle_health(None)
                self.assertEqual(health.status, 503)

    async def test_successful_empty_poll_refreshes_health(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.api.panel_http.aclose)
        self.addAsyncCleanup(bot.api.telegram_http.aclose)
        request = bot.api.WatchedRequest(proxy=bot.config.TG_PROXY)
        self.addAsyncCleanup(request.shutdown)
        with patch.object(bot.api.HTTPXRequest, 'do_request', AsyncMock(
                return_value=(200, json.dumps({'ok': True, 'result': []}).encode()))):
            stale = time.monotonic() - bot.config.POLL_STALL_SEC - 10
            bot.api.LAST_POLL_OK = stale
            await request.do_request(url='https://api.telegram.org/test', method='POST')
            self.assertGreater(bot.api.LAST_POLL_OK, stale)
            self.assertEqual((await bot.events.handle_health(None)).status, 200)

    async def test_health_reports_bot_username(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.api.panel_http.aclose)
        self.addAsyncCleanup(bot.api.telegram_http.aclose)
        bot.api.LAST_POLL_OK = time.monotonic()
        bot.config.BOT_USERNAME = 'example_vpn_bot'
        health = await bot.events.handle_health(None)
        self.assertEqual(json.loads(health.body)['username'], 'example_vpn_bot')
        bot.config.BOT_USERNAME = None
        health = await bot.events.handle_health(None)
        self.assertEqual(json.loads(health.body)['username'], '')

    async def test_stalled_watchdog_exits(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.api.panel_http.aclose)
        self.addAsyncCleanup(bot.api.telegram_http.aclose)
        bot.api.LAST_POLL_OK = time.monotonic() - bot.config.POLL_STALL_SEC - 10
        with patch.object(bot.api.asyncio, 'sleep', AsyncMock()), \
                patch.object(bot.api.os, '_exit', side_effect=asyncio.CancelledError) as exit_process:
            with self.assertRaises(asyncio.CancelledError):
                await bot.api.poll_watchdog()
            exit_process.assert_called_once_with(1)


if __name__ == '__main__':
    unittest.main()
