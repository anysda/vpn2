import unittest
from unittest.mock import AsyncMock, patch

import httpx

from support import load_bot, update


class DevicePathTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_cannot_change_api_route_or_owner(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        requests = []

        def panel(request):
            requests.append(str(request.url))
            return httpx.Response(200, json={'devices': []})

        async with httpx.AsyncClient(base_url='http://panel.test',
                                     transport=httpx.MockTransport(panel)) as client:
            for arg in ('../admin/clients/7', '7?chatId=999#', '%2e%2e/admin/clients/7',
                        '7/../8', '7#fragment', '', '-1', '7?chatId=999&unused='):
                with self.subTest(arg=arg), patch.object(bot, '_local', client), \
                        patch.object(bot, 'tg_answer_callback', AsyncMock()), \
                        patch.object(bot, 'tg_edit', AsyncMock()):
                    requests.clear()
                    await bot.on_callback(update(data=f'delyes:{arg}'), None)
                    self.assertEqual(requests, [], 'untrusted ID changed an authenticated API URL')

    async def test_all_device_api_helpers_reject_url_syntax(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        for helper in (bot.api_delete_device, bot.api_reissue_device,
                       bot.api_wg_config, bot.api_ovpn_config):
            with self.subTest(helper=helper.__name__), \
                    patch.object(bot, '_bot_request', AsyncMock()) as request:
                with self.assertRaises(bot.BotApiError):
                    await helper(200, '7?chatId=999#')
                request.assert_not_awaited()

    async def test_valid_device_keeps_callers_chat_id(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        with patch.object(bot, '_bot_request', AsyncMock(return_value=httpx.Response(
                200, json={'devices': []}))) as request:
            await bot.api_delete_device(200, '7')
            request.assert_awaited_once_with('DELETE', '/api/bot/devices/7?chatId=200')


if __name__ == '__main__':
    unittest.main()
