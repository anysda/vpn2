import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from support import load_bot


class AlertDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_telegram_message_is_an_error(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        for status in (403, 429, 500):
            with self.subTest(status=status), patch.object(bot._tg, 'post', AsyncMock(
                    return_value=httpx.Response(status, json={
                        'ok': False, 'description': 'unavailable', 'error_code': status}))):
                with self.assertRaises(Exception):
                    await bot.tg_send(100, 'alert')

    async def test_load_alert_and_recovery_retry_after_send_failure(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        bot.ALERT_HITS = 1
        for recovering in (False, True):
            with self.subTest(recovering=recovering):
                bot._load_alerted.clear()
                if recovering:
                    bot._load_alerted.add('test:cpu')
                value = 10 if recovering else 99
                with patch.object(bot, 'tg_send', AsyncMock(side_effect=[
                        httpx.ConnectError('offline'), {'ok': True}])) as send:
                    with self.assertRaises(httpx.ConnectError):
                        await bot._check_load('test', 'cpu', value, 80, 'CPU')
                    await bot._check_load('test', 'cpu', value, 80, 'CPU')
                    self.assertEqual(send.await_count, 2)
                    self.assertEqual('test:cpu' in bot._load_alerted, not recovering)

    async def test_node_alert_and_recovery_retry_after_send_failure(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        bot.ALERT_HITS = 1
        for recovering in (False, True):
            with self.subTest(recovering=recovering):
                bot._node_alerted.clear()
                if recovering:
                    bot._node_alerted.add('test')
                snapshot = {'nodes': [{'tag': 'test', 'staleSec': 0 if recovering else None}]}
                with patch.object(bot, 'api_status', AsyncMock(return_value=snapshot)), \
                        patch.object(bot.asyncio, 'sleep', AsyncMock(side_effect=[
                            None, None, asyncio.CancelledError()])), \
                        patch.object(bot, 'tg_send', AsyncMock(side_effect=[
                            httpx.ConnectError('offline'), {'ok': True}])) as send:
                    with self.assertRaises(asyncio.CancelledError):
                        await bot.monitor_loop()
                    self.assertEqual(send.await_count, 2)
                    self.assertEqual('test' in bot._node_alerted, not recovering)

    async def test_markdown_fallback_still_delivers(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        with patch.object(bot._tg, 'post', AsyncMock(side_effect=[
                httpx.Response(400, json={'ok': False, 'description': "can't parse entities"}),
                httpx.Response(200, json={'ok': True}),
        ])) as send:
            self.assertTrue((await bot.tg_send(100, '*broken', 'Markdown'))['ok'])
            self.assertNotIn('parse_mode', send.call_args.kwargs['json'])


if __name__ == '__main__':
    unittest.main()
