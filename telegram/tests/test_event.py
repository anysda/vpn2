import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from support import import_bot


class EventInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_deploy_event_passes_warning_to_announcement(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        for payload, expected in (({}, False), ({'verify_unmeasured': True}, True)):
            request = SimpleNamespace(headers={'X-Tgbot-Secret': bot.config.SECRET}, json=AsyncMock(
                return_value={'type': 'deploy_done', **payload}))
            with patch.object(bot.events, '_announce_deploy', AsyncMock()) as announce, \
                    patch.object(bot.events.asyncio, 'create_task', side_effect=lambda coro: coro.close()):
                self.assertEqual((await bot.events.handle_event(request)).status, 200)
                announce.assert_called_once_with(expected)

    async def test_deploy_warning_survives_status_api_failure(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        for unmeasured in (False, True):
            for api_failed in (False, True):
                with self.subTest(unmeasured=unmeasured, api_failed=api_failed):
                    async def status(title):
                        if api_failed:
                            raise RuntimeError('fixture')
                        return title + '\nmetrics'
                    with patch.object(bot.monitor, 'build_status_text', side_effect=status), \
                            patch.object(bot.events.asyncio, 'sleep', AsyncMock()), \
                            patch.object(bot.transport, 'tg_send', AsyncMock()) as send:
                        await bot.events._announce_deploy(unmeasured)
                    text = send.await_args.args[1]
                    self.assertEqual('с предупреждениями' in text, unmeasured)
                    self.assertEqual('Не проверено:' in text, unmeasured)
                    self.assertEqual('✅' in text, not unmeasured)

    async def test_non_object_json_returns_bad_request(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        for payload in (None, [], 'text', 42, True):
            with self.subTest(payload=payload):
                request = SimpleNamespace(headers={'X-Tgbot-Secret': bot.config.SECRET},
                                          json=AsyncMock(return_value=payload))
                response = await bot.events.handle_event(request)
                self.assertEqual(response.status, 400)

    async def test_invalid_secret_is_rejected_before_reading_body(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        for secret in ('', 'incorrect', bot.config.SECRET + 'x', 'неверный'):
            request = SimpleNamespace(headers={'X-Tgbot-Secret': secret}, json=AsyncMock())
            self.assertEqual((await bot.events.handle_event(request)).status, 403)
            request.json.assert_not_awaited()

    async def test_authenticated_event_is_queued(self):
        bot = import_bot()
        self.addAsyncCleanup(bot.transport._local.aclose)
        self.addAsyncCleanup(bot.transport._tg.aclose)
        queue = SimpleNamespace(enqueue=lambda *_args: 1)
        request = SimpleNamespace(app={bot.events.OUTBOX_KEY: queue}, headers={'X-Tgbot-Secret': bot.config.SECRET}, json=AsyncMock(
            return_value={'type': 'client_created', 'name': 'test'}))
        with patch.object(bot.transport, 'tg_send', AsyncMock()) as send:
            self.assertEqual((await bot.events.handle_event(request)).status, 200)
            send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
