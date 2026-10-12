import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from support import load_bot


class PollHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_poll_cannot_keep_health_green(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        request = bot._WatchedRequest(proxy=bot.TG_PROXY)
        self.addAsyncCleanup(request.shutdown)
        responses = [(502, b'upstream down'), (409, b'{"ok":false}'),
                     (200, b'{"ok":false}'), (200, b'invalid json'),
                     (200, b'[]'), (200, b'{"ok":true}')]
        for response in responses:
            with self.subTest(response=response), patch.object(
                    bot.HTTPXRequest, 'do_request', AsyncMock(return_value=response)):
                bot.LAST_POLL_OK = 0
                await request.do_request(url='https://api.telegram.org/test', method='POST')
                self.assertEqual(bot.LAST_POLL_OK, 0)
                health = await bot.handle_health(None)
                self.assertEqual(health.status, 503)

    async def test_successful_empty_poll_refreshes_health(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        request = bot._WatchedRequest(proxy=bot.TG_PROXY)
        self.addAsyncCleanup(request.shutdown)
        with patch.object(bot.HTTPXRequest, 'do_request', AsyncMock(
                return_value=(200, json.dumps({'ok': True, 'result': []}).encode()))):
            bot.LAST_POLL_OK = 0
            await request.do_request(url='https://api.telegram.org/test', method='POST')
            self.assertGreater(bot.LAST_POLL_OK, 0)
            self.assertEqual((await bot.handle_health(None)).status, 200)

    async def test_stalled_watchdog_exits(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        bot.LAST_POLL_OK = 0
        with patch.object(bot.asyncio, 'sleep', AsyncMock()), \
                patch.object(bot.os, '_exit', side_effect=asyncio.CancelledError) as exit_process:
            with self.assertRaises(asyncio.CancelledError):
                await bot.poll_watchdog()
            exit_process.assert_called_once_with(1)


if __name__ == '__main__':
    unittest.main()
