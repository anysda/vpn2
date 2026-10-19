"""Ошибки /api/bot/* доходят до человека текстом, а не машинным кодом панели."""
import re
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from support import load_bot

# Всё, что обработчики /api/bot/** и их утилиты кладут в statusMessage.
PANEL_CODES = (
    'client_exists', 'device_exists', 'device_limit', 'device_limit_below_count',
    'ikev2_pool_exhausted', 'insert_failed', 'invalid_body', 'invalid_chat_id',
    'invalid_expiry', 'invalid_id', 'invalid_json', 'invalid_origin', 'invalid_params',
    'invalid_password', 'loopback_only', 'not_found', 'not_linked', 'outbounds_unavailable',
    'ovpn_ca_not_ready', 'ovpn_disabled', 'ovpn_pool_exhausted',
    'ovpn_public_host_not_configured', 'ovpn_reissue_failed', 'tgbot_secret_not_configured',
    'unauthorized', 'unknown_outbound', 'wg_disabled', 'wg_ipv4_endpoint_unavailable',
    'wg_keys_not_ready', 'wg_pool_exhausted', 'wg_public_host_not_configured',
)
CODE_LIKE = re.compile(r'\b[a-z0-9]+(?:_[a-z0-9]+)+\b')


def nitro(status, code, message=None):
    """Тело ошибки, как его отдаёт Nitro: без своего текста message равен коду."""
    return httpx.Response(status, json={'error': True, 'statusCode': status,
                                        'statusMessage': code, 'message': message or code})


class ErrorTextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):  # noqa - unittest lifecycle hook, called by the framework
        self.bot = load_bot()
        self.addAsyncCleanup(self.bot._local.aclose)
        self.addAsyncCleanup(self.bot._tg.aclose)

    def assertHuman(self, text, code):
        self.assertNotIn(code, text)
        self.assertIsNone(CODE_LIKE.search(text), text)
        self.assertRegex(text, '[А-Яа-яЁё]')

    async def test_every_panel_code_becomes_text(self):
        for code in PANEL_CODES:
            for status in (400, 403, 404, 409, 422):
                with self.subTest(code=code, status=status):
                    error = self.bot._extract_error(nitro(status, code))
                    self.assertHuman(error.message, code)
                    self.assertEqual(error.code, code)

    async def test_admin_text_of_panel_is_replaced_for_client(self):
        error = self.bot._extract_error(nitro(403, 'loopback_only', 'API бота доступен только с этой ноды'))
        self.assertNotIn('ноды', error.message)
        self.assertHuman(error.message, 'loopback_only')

    async def test_handler_text_passes_through(self):
        error = self.bot._extract_error(httpx.Response(409, json={'statusCode': 409, 'message': 'Клиент удаляется'}))
        self.assertEqual(error.message, 'Клиент удаляется')

    async def test_unknown_code_gets_generic_text_and_goes_to_log(self):
        for response in (nitro(409, 'device_limit_reached'),
                         httpx.Response(409, json={'message': 'device_limit_reached'})):
            with self.subTest(body=response.text), self.assertLogs('tgbot', 'WARNING') as logs:
                error = self.bot._extract_error(response)
                self.assertHuman(error.message, 'device_limit_reached')
                self.assertIn('device_limit_reached', '\n'.join(logs.output))

    async def test_client_reply_has_no_code(self):
        with patch.object(self.bot, 'tg_send', AsyncMock()) as send:
            await self.bot._client_error_reply(200, self.bot._extract_error(nitro(409, 'device_exists')))
        self.assertHuman(send.await_args.args[1], 'device_exists')

    async def test_not_linked_is_still_recognised(self):
        for response in (nitro(404, 'not_linked'), httpx.Response(404, json={'message': 'not_linked'})):
            with self.subTest(body=response.text):
                self.assertTrue(self.bot._is_not_linked(self.bot._extract_error(response)))


if __name__ == '__main__':
    unittest.main()
