import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from support import load_bot


DOWN = '🔴 Панель недоступна — мониторинговые данные не поступают'
UP = '🟢 Панель снова доступна'


class PanelAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def check_polls(self, polls, expected, send_errors=None):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        clock = 0
        remaining = iter(polls)

        async def poll():
            nonlocal clock
            try:
                clock, available = next(remaining)
            except StopIteration:
                raise asyncio.CancelledError() from None
            if not available:
                raise httpx.ConnectError('panel unavailable')
            return {'nodes': []}

        with patch.object(bot, 'api_status', poll), \
                patch.object(bot, 'time', SimpleNamespace(monotonic=lambda: clock)), \
                patch.object(bot.asyncio, 'sleep', AsyncMock()), \
                patch.object(bot, 'tg_send', AsyncMock(side_effect=send_errors)) as send:
            with self.assertRaises(asyncio.CancelledError):
                await bot.monitor_loop()
        self.assertEqual([call.args[1] for call in send.await_args_list], expected)

    async def test_five_minutes_exactly_one_alert_and_one_recovery(self):
        await self.check_polls(
            [(0, False), (299, False), (300, False), (600, False), (630, True), (660, True)],
            [DOWN, UP])

    async def test_short_outages_do_not_accumulate(self):
        await self.check_polls(
            [(0, False), (299, False), (300, True), (301, False), (600, False), (601, True)], [])

    async def test_new_outage_alerts_again(self):
        await self.check_polls(
            [(0, False), (300, False), (330, True), (400, False), (700, False), (730, False)],
            [DOWN, UP, DOWN])

    async def test_failed_alert_and_recovery_are_retried(self):
        await self.check_polls(
            [(0, False), (300, False), (330, False), (360, True), (390, True), (420, True)],
            [DOWN, DOWN, UP, UP], [httpx.ConnectError('telegram'), {}, httpx.ConnectError('telegram'), {}])


if __name__ == '__main__':
    unittest.main()
