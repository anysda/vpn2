import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from support import load_bot


class EventInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_object_json_returns_bad_request(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        for payload in (None, [], 'text', 42, True):
            with self.subTest(payload=payload):
                request = SimpleNamespace(headers={'X-Tgbot-Secret': bot.SECRET},
                                          json=AsyncMock(return_value=payload))
                response = await bot.handle_event(request)
                self.assertEqual(response.status, 400)

    async def test_invalid_secret_is_rejected_before_reading_body(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        for secret in ('', 'incorrect', bot.SECRET + 'x', 'неверный'):
            request = SimpleNamespace(headers={'X-Tgbot-Secret': secret}, json=AsyncMock())
            self.assertEqual((await bot.handle_event(request)).status, 403)
            request.json.assert_not_awaited()

    async def test_authenticated_event_is_queued(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        queue = SimpleNamespace(enqueue=lambda *_args: 1)
        request = SimpleNamespace(app={bot.OUTBOX_KEY: queue}, headers={'X-Tgbot-Secret': bot.SECRET}, json=AsyncMock(
            return_value={'type': 'client_created', 'name': 'test'}))
        with patch.object(bot, 'tg_send', AsyncMock()) as send:
            self.assertEqual((await bot.handle_event(request)).status, 200)
            send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
