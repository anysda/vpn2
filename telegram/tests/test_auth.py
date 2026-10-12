import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from support import load_bot, update


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_member_cannot_use_admin_handlers(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        bot.CHAT_ID = -100
        for handler, data in ((bot.cmd_start, None), (bot.cmd_status, None),
                              (bot.cmd_nodes, None), (bot.cmd_clients, None),
                              (bot.on_text, None), (bot.on_callback, 'adyes:7')):
            with self.subTest(handler=handler.__name__), \
                    patch.object(bot, '_tg_post', AsyncMock(return_value={
                        'ok': True, 'result': {'status': 'member'}})), \
                    patch.object(bot, 'tg_send', AsyncMock()) as send, \
                    patch.object(bot, 'api_status', AsyncMock(return_value={})), \
                    patch.object(bot, 'cmd_clients_interactive', AsyncMock()) as clients, \
                    patch.object(bot, '_handle_admin_text', AsyncMock()) as text, \
                    patch.object(bot, '_handle_admin_callback', AsyncMock()) as callback:
                await handler(update(-100, 200, 'supergroup', data), SimpleNamespace(args=[]))
                send.assert_not_awaited()
                clients.assert_not_awaited()
                text.assert_not_awaited()
                callback.assert_not_awaited()

    async def test_client_group_cannot_link_or_operate_devices(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        for handler, data in ((bot.cmd_start, None), (bot.on_text, None),
                              (bot.on_callback, 'delyes:7')):
            with self.subTest(handler=handler.__name__), \
                    patch.object(bot, 'api_link', AsyncMock(return_value={})) as link, \
                    patch.object(bot, 'api_client', AsyncMock(return_value={})) as client, \
                    patch.object(bot, '_handle_callback', AsyncMock()) as callback, \
                    patch.object(bot, 'tg_send', AsyncMock()):
                await handler(update(-200, 200, 'group', data), SimpleNamespace(args=['password']))
                link.assert_not_awaited()
                client.assert_not_awaited()
                callback.assert_not_awaited()

    async def test_group_administrator_and_private_owner_still_work(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        for admin_chat, actor, chat_type in ((100, 100, 'private'), (-100, 200, 'supergroup')):
            bot.CHAT_ID = admin_chat
            with patch.object(bot, '_tg_post', AsyncMock(return_value={
                    'ok': True, 'result': {'status': 'administrator'}})), \
                    patch.object(bot, '_handle_admin_callback', AsyncMock()) as callback:
                await bot.on_callback(update(admin_chat, actor, chat_type, 'acl'), None)
                callback.assert_awaited_once()
        with patch.object(bot, '_handle_callback', AsyncMock()) as callback:
            await bot.on_callback(update(data='wg:7'), None)
            callback.assert_awaited_once_with(200, 1, 'callback', 'wg:7')

    async def test_membership_lookup_failure_denies_access(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        bot.CHAT_ID = -100
        with patch.object(bot, '_tg_post', AsyncMock(side_effect=TimeoutError)), \
                patch.object(bot, '_handle_admin_callback', AsyncMock()) as callback:
            await bot.on_callback(update(-100, 200, 'supergroup', 'adyes:7'), None)
            callback.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
