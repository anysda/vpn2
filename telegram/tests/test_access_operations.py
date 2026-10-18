"""Принятое изменение доступа не выдаётся за подтверждённое."""
import unittest
from unittest.mock import AsyncMock, patch

from support import load_bot


class AccessOperationsTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_acceptance_is_not_final_success(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        for action, expected in (('afz:1', 'Отключение запущено'),
                                 ('adyes:1', 'Отключение запущено')):
            with self.subTest(action=action), \
                    patch.object(bot, 'api_admin_client', AsyncMock(return_value={'id': 1})), \
                    patch.object(bot, 'api_admin_patch_client', AsyncMock(return_value={'operation': {'status': 'pending'}})), \
                    patch.object(bot, 'api_admin_delete_client', AsyncMock(return_value={'operation': {'status': 'pending'}})), \
                    patch.object(bot, '_admin_show_card', AsyncMock()), \
                    patch.object(bot, '_admin_show_list', AsyncMock()), \
                    patch.object(bot, 'tg_answer_callback', AsyncMock()) as answer:
                await bot._handle_admin_callback(1, 'callback', action)
                self.assertEqual(answer.await_args.args[1], expected)

    async def test_admin_card_uses_verified_status_from_api(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        text = bot._admin_card_text({'name': 'test', 'status': 'frozen',
                                    'accessStatus': 'Отключение не завершено: OpenVPN'})
        self.assertIn('Отключение не завершено: OpenVPN', text)
        self.assertNotIn('Доступ отключён', text)


if __name__ == '__main__':
    unittest.main()
