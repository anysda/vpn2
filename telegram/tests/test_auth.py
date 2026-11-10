import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from support import import_bot, update


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_member_cannot_use_admin_handlers(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        bot.config.CHAT_ID = -100
        for handler, data in ((bot.client.cmd_start, None), (bot.monitor.cmd_status, None),
                              (bot.monitor.cmd_nodes, None), (bot.monitor.cmd_clients, None),
                              (bot.client.on_text, None), (bot.client.on_callback, 'adyes:7')):
            with self.subTest(handler=handler.__name__), \
                    patch.object(bot.transport, '_tg_post', AsyncMock(return_value={
                        'ok': True, 'result': {'status': 'member'}})), \
                    patch.object(bot.transport, 'tg_send', AsyncMock()) as send, \
                    patch.object(bot.panel, 'api_status', AsyncMock(return_value={})), \
                    patch.object(bot.admin, 'cmd_clients_interactive', AsyncMock()) as clients, \
                    patch.object(bot.admin, '_handle_admin_text', AsyncMock()) as text, \
                    patch.object(bot.admin, '_handle_admin_callback', AsyncMock()) as callback:
                await handler(update(-100, 200, 'supergroup', data), SimpleNamespace(args=[]))
                send.assert_not_awaited()
                clients.assert_not_awaited()
                text.assert_not_awaited()
                callback.assert_not_awaited()

    async def test_client_group_cannot_link_or_operate_devices(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        for handler, data in ((bot.client.cmd_start, None), (bot.client.on_text, None),
                              (bot.client.on_callback, 'delyes:7')):
            with self.subTest(handler=handler.__name__), \
                    patch.object(bot.panel, 'api_link', AsyncMock(return_value={})) as link, \
                    patch.object(bot.panel, 'api_client', AsyncMock(return_value={})) as client, \
                    patch.object(bot.client, '_handle_callback', AsyncMock()) as callback, \
                    patch.object(bot.transport, 'tg_send', AsyncMock()):
                await handler(update(-200, 200, 'group', data), SimpleNamespace(args=['password']))
                link.assert_not_awaited()
                client.assert_not_awaited()
                callback.assert_not_awaited()

    async def test_group_administrator_and_private_owner_still_work(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        for admin_chat, actor, chat_type in ((100, 100, 'private'), (-100, 200, 'supergroup')):
            bot.config.CHAT_ID = admin_chat
            with patch.object(bot.transport, '_tg_post', AsyncMock(return_value={
                    'ok': True, 'result': {'status': 'administrator'}})), \
                    patch.object(bot.admin, '_handle_admin_callback', AsyncMock()) as callback:
                await bot.client.on_callback(update(admin_chat, actor, chat_type, 'acl'), None)
                callback.assert_awaited_once()
        with patch.object(bot.client, '_handle_callback', AsyncMock()) as callback:
            await bot.client.on_callback(update(data='wg:7'), None)
            callback.assert_awaited_once_with(200, 1, 'callback', 'wg:7')

    async def test_membership_lookup_failure_denies_access(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        bot.config.CHAT_ID = -100
        with patch.object(bot.transport, '_tg_post', AsyncMock(side_effect=TimeoutError)), \
                patch.object(bot.admin, '_handle_admin_callback', AsyncMock()) as callback:
            await bot.client.on_callback(update(-100, 200, 'supergroup', 'adyes:7'), None)
            callback.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
