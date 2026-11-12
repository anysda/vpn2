import asyncio
from contextlib import suppress
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from harness import import_bot


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):  # noqa - unittest lifecycle hook, called by the framework
        # One fresh package per test: the queue, its exceptions and 'vpn2bot.outbox.X' patches share a module.
        self.bot = import_bot()
        self.addAsyncCleanup(self.bot.api.panel_http.aclose)
        self.addAsyncCleanup(self.bot.api.telegram_http.aclose)
        self.Outbox, self.QueueFull = self.bot.outbox.Outbox, self.bot.outbox.QueueFull
        self.TelegramSendError = self.bot.outbox.TelegramSendError
        directory = tempfile.TemporaryDirectory(prefix='tgbot-outbox-')
        self.addCleanup(directory.cleanup)
        self.queue = self.open(directory.name + '/outbox.sqlite')

    def open(self, path):
        queue = self.Outbox(path)
        self.addCleanup(queue.db.close)
        return queue

    def rows(self):
        return self.queue.db.execute('SELECT id, attempts, due FROM events ORDER BY id').fetchall()

    def event(self, **body):
        return SimpleNamespace(headers={'X-Tgbot-Secret': self.bot.config.SECRET},
                               app={self.bot.events.OUTBOX_KEY: self.queue}, json=AsyncMock(return_value=body))

    async def worker_until(self, send, predicate, rejected=None):
        worker = asyncio.create_task(self.queue.run(send, rejected or AsyncMock()))
        try:
            async with asyncio.timeout(5):
                while not predicate():
                    await asyncio.sleep(0.01)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def test_acceptance_is_committed_and_storage_failure_is_not_200(self):
        request = self.event(type='client_notify', chatId=200, text='durable')
        self.assertEqual((await self.bot.events.handle_event(request)).status, 200)
        self.assertEqual(len(self.rows()), 1)
        self.assertIn('durable', self.open(self.queue.path).claim()['payload'])
        for error in (sqlite3.OperationalError('disk full'), self.QueueFull()):
            with patch.object(self.queue, 'enqueue', side_effect=error):
                response = await self.bot.events.handle_event(request)
                self.assertEqual((response.status, response.text), (503, 'notification queue unavailable'))

    async def test_panel_retry_after_lost_ack_and_bot_restart_delivers_once(self):
        request = self.event(type='client_notify', chatId=200, text='once', deliveryId='retry-test')
        self.assertEqual((await self.bot.events.handle_event(request)).status, 200)
        send = AsyncMock()
        await self.worker_until(send, lambda: not self.rows())
        self.queue = self.open(self.queue.path)
        self.assertEqual((await self.bot.events.handle_event(self.event(**request.json.return_value))).status, 200)
        self.assertEqual(self.rows(), [])
        send.assert_awaited_once()

    async def test_broadcast_is_atomic_and_deduplicated(self):
        request = self.event(type='broadcast', chatIds=[200, 300, 200], text='broadcast', deliveryId='b')
        with patch('vpn2bot.outbox.MAX_EVENTS', 1):
            self.assertEqual((await self.bot.events.handle_event(request)).status, 503)
        self.assertEqual(self.rows(), [])
        for _ in range(2):
            self.assertEqual((await self.bot.events.handle_event(request)).status, 200)
        self.assertEqual(len(self.rows()), 2)
        send = AsyncMock()
        await self.worker_until(send, lambda: not self.rows())
        self.assertCountEqual([call.kwargs['chat_id'] for call in send.await_args_list], [200, 300])

    def test_receipt_is_bound_to_payload(self):
        payload = {'chat_id': 200, 'text': 'original'}
        event_id = self.queue.enqueue([payload], 'stable-id')
        self.queue.remove(event_id)
        self.assertEqual(self.open(self.queue.path).enqueue([payload], 'stable-id'), event_id)
        with self.assertRaises(ValueError):
            self.queue.enqueue([{'chat_id': 200, 'text': 'different'}], 'stable-id')
        self.assertEqual(self.rows(), [])

    async def test_validation_and_bounded_queue_expiry(self):
        for text in ('', [], 'x' * 4097, '\U0001f600' * 2049):
            with self.assertRaises(ValueError):
                self.queue.enqueue([{'chat_id': 200, 'text': text}])
        with patch('vpn2bot.outbox.MAX_EVENTS', 2), patch('vpn2bot.outbox.time.time', return_value=100):
            self.queue.enqueue([{'chat_id': 200, 'text': 'a'}])
            self.queue.enqueue([{'chat_id': 200, 'text': 'b'}])
            with self.assertRaises(self.QueueFull):
                self.queue.enqueue([{'chat_id': 200, 'text': 'c'}])
        with patch('vpn2bot.outbox.time.time', return_value=100 + 86400):
            self.assertIsNone(self.queue.claim())
        self.assertEqual(self.rows(), [])

    async def test_lease_survives_reopen_and_preserves_chat_order(self):
        with patch('vpn2bot.outbox.time.time', return_value=100):
            first = self.queue.enqueue([{'chat_id': 200, 'text': 'first'}])
            second = self.queue.enqueue([{'chat_id': 200, 'text': 'second'}])
            other = self.queue.enqueue([{'chat_id': 300, 'text': 'other'}])
            self.assertEqual(self.queue.claim()['id'], first)
            reopened = self.open(self.queue.path)
            self.assertEqual(reopened.claim()['id'], other)
            self.assertIsNone(reopened.claim())
        with patch('vpn2bot.outbox.time.time', return_value=221):
            self.assertEqual(reopened.claim()['id'], first)
            reopened.remove(first)
            self.assertEqual(reopened.claim()['id'], second)

    async def test_success_is_not_resent_when_ack_commit_fails(self):
        self.queue.enqueue([{'chat_id': 200, 'text': 'once'}])
        send = AsyncMock()
        remove = self.queue.remove
        with patch.object(self.queue, 'remove', side_effect=[sqlite3.OperationalError('busy'), None]) as ack:
            await self.worker_until(send, lambda: ack.call_count == 2)
        send.assert_awaited_once()
        remove(self.rows()[0][0])

    async def test_backoff_ambiguous_send_and_permanent_error(self):
        for error, minimum in ((self.TelegramSendError(503), 5), (TimeoutError(), 60)):
            event_id = self.queue.enqueue([{'chat_id': 200, 'text': 'retry'}])
            now = time.time()
            send = AsyncMock(side_effect=error)
            await self.worker_until(send, lambda: bool(self.rows()) and self.rows()[0][2] < now + 100
                                    and self.rows()[0][1] == 1)
            self.assertGreaterEqual(self.rows()[0][2], now + minimum)
            send.assert_awaited_once()
            self.queue.remove(event_id)
        self.queue.enqueue([{'chat_id': 200, 'text': 'blocked recipient'}])
        await self.worker_until(AsyncMock(side_effect=self.TelegramSendError(403)), lambda: not self.rows())

    async def test_permanent_rejection_is_reported_once_and_report_failure_is_harmless(self):
        reported = AsyncMock(side_effect=[RuntimeError('panel down'), None])
        for text in ('first', 'second'):
            self.queue.enqueue([{'chat_id': 200, 'text': text}])
            await self.worker_until(AsyncMock(side_effect=self.TelegramSendError(403)),
                                    lambda: not self.rows(), reported)
        self.assertEqual(reported.await_args_list[0].args, ({'chat_id': 200, 'text': 'first'}, 403))
        self.assertEqual(reported.await_count, 2)
        self.queue.enqueue([{'chat_id': 200, 'text': 'retry'}])
        await self.worker_until(AsyncMock(side_effect=self.TelegramSendError(503)),
                                lambda: self.rows()[0][1] == 1, reported)
        self.assertEqual(reported.await_count, 2)

    async def test_bot_reports_rejection_to_panel_journal(self):
        bot = self.bot
        response = Mock(raise_for_status=Mock())
        with patch.object(bot.api.panel_http, 'post', AsyncMock(return_value=response)) as post:
            await bot.events.report_rejected({'chat_id': bot.config.CHAT_ID, 'text': 'x'}, 400)
            await bot.events.report_rejected({'chat_id': bot.config.CHAT_ID + 1, 'text': 'x'}, 403)
        self.assertEqual([c.args[0] for c in post.await_args_list], ['/api/bot/events'] * 2)
        self.assertEqual([c.kwargs['json'] for c in post.await_args_list], [
            {'type': 'telegram_rejected', 'status': 400, 'recipient': 'admin'},
            {'type': 'telegram_rejected', 'status': 403, 'recipient': 'client'},
        ])
        self.assertTrue(bot.api.panel_http.headers['Authorization'].startswith('Bearer '))

    async def test_retry_after_pauses_all_recipients(self):
        self.queue.enqueue([{'chat_id': 200, 'text': 'rate limited'}])
        send = AsyncMock(side_effect=self.TelegramSendError(429, 600))
        await self.worker_until(send, lambda: self.rows()[0][2] > time.time() + 500)
        self.queue.enqueue([{'chat_id': 300, 'text': 'new recipient'}])
        self.assertIsNone(self.queue.claim())
        send.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
