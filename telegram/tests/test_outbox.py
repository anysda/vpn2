import asyncio
from contextlib import closing, suppress
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from support import load_bot
from outbox import Outbox, QueueFull, TelegramSendError


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):  # noqa - unittest lifecycle hook, called by the framework
        directory = tempfile.TemporaryDirectory(prefix='tgbot-outbox-')
        self.addCleanup(directory.cleanup)
        self.queue = Outbox(directory.name + '/outbox.sqlite')

    def rows(self):
        with closing(self.queue.connect()) as db:
            return db.execute('SELECT id, attempts, due FROM events ORDER BY id').fetchall()

    async def worker_until(self, send, predicate):
        worker = asyncio.create_task(self.queue.run(send))
        try:
            async with asyncio.timeout(5):
                while not predicate():
                    await asyncio.sleep(0.01)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def test_acceptance_is_committed_and_storage_failure_is_not_200(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        request = SimpleNamespace(
            headers={'X-Tgbot-Secret': bot.SECRET}, app={bot.OUTBOX_KEY: self.queue},
            json=AsyncMock(return_value={'type': 'client_notify', 'chatId': 200, 'text': 'durable'}))
        self.assertEqual((await bot.handle_event(request)).status, 200)
        self.assertEqual(len(self.rows()), 1)
        reopened = Outbox(self.queue.path)
        self.assertIn('durable', reopened.claim()['payload'])
        for error in (sqlite3.OperationalError('disk full'), QueueFull()):
            with patch.object(self.queue, 'enqueue', side_effect=error):
                self.assertEqual((await bot.handle_event(request)).status, 503)

    async def test_panel_retry_after_lost_ack_and_bot_restart_delivers_once(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        request = SimpleNamespace(
            headers={'X-Tgbot-Secret': bot.SECRET}, app={bot.OUTBOX_KEY: self.queue},
            json=AsyncMock(return_value={'type': 'client_notify', 'chatId': 200,
                                        'text': 'once', 'deliveryId': 'retry-test'}))
        self.assertEqual((await bot.handle_event(request)).status, 200)
        send = AsyncMock()
        await self.worker_until(send, lambda: not self.rows())
        request.app[bot.OUTBOX_KEY] = Outbox(self.queue.path)
        self.assertEqual((await bot.handle_event(request)).status, 200)
        self.assertEqual(self.rows(), [])
        send.assert_awaited_once()

    async def test_broadcast_is_atomic_and_deduplicated(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        request = SimpleNamespace(
            headers={'X-Tgbot-Secret': bot.SECRET}, app={bot.OUTBOX_KEY: self.queue},
            json=AsyncMock(return_value={'type': 'broadcast', 'chatIds': [200, 300, 200],
                                        'text': 'broadcast', 'deliveryId': 'broadcast-test'}))
        with patch('outbox.MAX_EVENTS', 1):
            self.assertEqual((await bot.handle_event(request)).status, 503)
        self.assertEqual(self.rows(), [])
        for _ in range(2):
            self.assertEqual((await bot.handle_event(request)).status, 200)
        self.assertEqual(len(self.rows()), 2)
        send = AsyncMock()
        await self.worker_until(send, lambda: not self.rows())
        self.assertEqual(send.await_count, 2)

    def test_receipt_is_bound_to_payload_and_never_evicted_for_capacity(self):
        payload = {'chat_id': 200, 'text': 'original'}
        with patch('outbox.MAX_RECEIPTS', 1):
            event_id = self.queue.enqueue(payload, 'stable-id')
            self.queue.remove(event_id)
            self.assertEqual(Outbox(self.queue.path).enqueue(payload, 'stable-id'), event_id)
            with self.assertRaises(ValueError):
                self.queue.enqueue({'chat_id': 200, 'text': 'different'}, 'stable-id')
            with self.assertRaises(QueueFull):
                self.queue.enqueue(payload, 'new-id')
            self.assertEqual(self.rows(), [])

    async def test_validation_and_bounded_queue_expiry(self):
        for text in ('', [], 'x' * 4097, '\U0001f600' * 2049):
            with self.assertRaises(ValueError):
                self.queue.enqueue({'chat_id': 200, 'text': text})
        with patch('outbox.MAX_EVENTS', 2), patch('outbox.time.time', return_value=100):
            self.queue.enqueue({'chat_id': 200, 'text': 'a'})
            self.queue.enqueue({'chat_id': 200, 'text': 'b'})
            with self.assertRaises(QueueFull):
                self.queue.enqueue({'chat_id': 200, 'text': 'c'})
        with patch('outbox.time.time', return_value=100 + 86400):
            self.assertIsNone(self.queue.claim())
        self.assertEqual(self.rows(), [])

    async def test_lease_survives_reopen_and_preserves_chat_order(self):
        with patch('outbox.time.time', return_value=100):
            first = self.queue.enqueue({'chat_id': 200, 'text': 'first'})
            second = self.queue.enqueue({'chat_id': 200, 'text': 'second'})
            other = self.queue.enqueue({'chat_id': 300, 'text': 'other'})
            self.assertEqual(self.queue.claim()['id'], first)
            reopened = Outbox(self.queue.path)
            self.assertEqual(reopened.claim()['id'], other)
            self.assertIsNone(reopened.claim())
        with patch('outbox.time.time', return_value=221):
            self.assertEqual(reopened.claim()['id'], first)
            reopened.remove(first)
            self.assertEqual(reopened.claim()['id'], second)

    async def test_success_is_not_resent_when_ack_commit_fails(self):
        self.queue.enqueue({'chat_id': 200, 'text': 'once'})
        send = AsyncMock()
        remove = self.queue.remove
        with patch.object(self.queue, 'remove', side_effect=[sqlite3.OperationalError('busy'), None]) as ack:
            await self.worker_until(send, lambda: ack.call_count == 2)
        send.assert_awaited_once()
        remove(self.rows()[0][0])

    async def test_backoff_ambiguous_send_and_permanent_error(self):
        for error, minimum in ((TelegramSendError(503), 5), (TimeoutError(), 60)):
            event_id = self.queue.enqueue({'chat_id': 200, 'text': 'retry'})
            now = time.time()
            send = AsyncMock(side_effect=error)
            await self.worker_until(send, lambda: bool(self.rows()) and self.rows()[0][2] < now + 100
                                    and self.rows()[0][1] == 1)
            self.assertGreaterEqual(self.rows()[0][2], now + minimum)
            send.assert_awaited_once()
            self.queue.remove(event_id)
        self.queue.enqueue({'chat_id': 200, 'text': 'blocked recipient'})
        await self.worker_until(AsyncMock(side_effect=TelegramSendError(403)), lambda: not self.rows())

    async def test_retry_after_pauses_all_recipients_and_survives_restart(self):
        self.queue.enqueue({'chat_id': 200, 'text': 'rate limited'})
        send = AsyncMock(side_effect=TelegramSendError(429, 600))
        await self.worker_until(send, lambda: self.rows()[0][2] > time.time() + 500)
        reopened = Outbox(self.queue.path)
        reopened.enqueue({'chat_id': 300, 'text': 'new recipient'})
        self.assertIsNone(reopened.claim())
        send.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
