"""Принятое изменение доступа не выдаётся за подтверждённое."""
import unittest
from unittest.mock import AsyncMock, patch

from support import import_bot


class AccessOperationsTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_acceptance_is_not_final_success(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        for action, expected in (('afz:1', 'Отключение запущено'),
                                 ('adyes:1', 'Отключение запущено')):
            with self.subTest(action=action), \
                    patch.object(bot.panel, 'api_admin_client', AsyncMock(return_value={'id': 1})), \
                    patch.object(bot.panel, 'api_admin_patch_client', AsyncMock(return_value={'operation': {'status': 'pending'}})), \
                    patch.object(bot.panel, 'api_admin_delete_client', AsyncMock(return_value={'operation': {'status': 'pending'}})), \
                    patch.object(bot.admin, '_admin_show_card', AsyncMock()), \
                    patch.object(bot.admin, '_admin_show_list', AsyncMock()), \
                    patch.object(bot.transport, 'tg_answer_callback', AsyncMock()) as answer:
                await bot.admin._handle_admin_callback(1, 'callback', action)
                self.assertEqual(answer.await_args.args[1], expected)

    async def test_admin_card_uses_verified_status_from_api(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        text = bot.admin._admin_card_text({'name': 'test', 'status': 'frozen',
                                    'accessStatus': 'Отключение не завершено: OpenVPN',
                                    'operation': {'protocols': {'wg': {'state': 'not_required'}}}})
        self.assertIn('Отключение не завершено: OpenVPN', text)
        self.assertIn('WireGuard: Не требуется', text)
        self.assertNotIn('Доступ отключён', text)

    async def test_admin_list_does_not_mark_pending_enable_as_success(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        row = bot.admin._admin_list_markup([{'id': 1, 'name': 'test', 'status': 'active',
                                      'accessStatus': 'Включение выполняется'}])['inline_keyboard'][0][0]
        self.assertIn('Включение выполняется', row['text'])
        self.assertNotIn('✅', row['text'])
        markup = bot.admin._admin_card_markup({'id': 1, 'accessStatus': 'Состояние не подтверждено',
                                         'operation': {'id': 'existing', 'status': 'confirmed'}})
        self.assertTrue(any(button.get('text') == 'Повторить'
                            for row in markup['inline_keyboard'] for button in row))


if __name__ == '__main__':
    unittest.main()
