import asyncio
from contextlib import closing, suppress
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

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

    async def test_broadcast_waits_for_concurrent_writer_and_delivery(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        self.queue.enqueue({'chat_id': 100, 'text': 'pending'})
        request = SimpleNamespace(
            headers={'X-Tgbot-Secret': bot.SECRET}, app={bot.OUTBOX_KEY: self.queue},
            json=AsyncMock(return_value={'type': 'broadcast', 'chatIds': [200, 300, 200],
                                        'text': 'broadcast', 'deliveryId': 'busy-broadcast'}))
        send = AsyncMock()
        with closing(self.queue.connect()) as writer, writer:
            # Model a slow rollback-journal commit: readers and writers must wait.
            writer.execute('BEGIN EXCLUSIVE')
            writer.execute('UPDATE throttle SET until = 0')
            accepting = asyncio.create_task(bot.handle_event(request))
            worker = asyncio.create_task(self.queue.run(send))
            try:
                await asyncio.sleep(0.5)
            finally:
                writer.commit()
        try:
            self.assertEqual((await accepting).status, 200)
            self.assertEqual((await bot.handle_event(request)).status, 200)
            async with asyncio.timeout(5):
                while await asyncio.to_thread(self.rows):
                    await asyncio.sleep(0.01)
            self.assertCountEqual(
                [call.kwargs for call in send.await_args_list],
                [{'chat_id': 100, 'text': 'pending'},
                 {'chat_id': 200, 'text': 'broadcast'},
                 {'chat_id': 300, 'text': 'broadcast'}])
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def test_write_transactions_wait_for_reserved_lock(self):
        event_id = self.queue.enqueue({'chat_id': 200, 'text': 'pending'})
        for operation, args in ((self.queue.enqueue, ({'chat_id': 300, 'text': 'new'},)),
                                (self.queue.claim, ()),
                                (self.queue.retry, (event_id, 60, True)),
                                (self.queue.remove, (event_id,))):
            with self.subTest(operation=operation.__name__):
                with closing(self.queue.connect()) as writer, writer:
                    writer.execute('BEGIN IMMEDIATE')
                    writer.execute('UPDATE throttle SET until = 0')
                    pending = asyncio.create_task(asyncio.to_thread(operation, *args))
                    try:
                        await asyncio.sleep(0.5)
                    finally:
                        writer.commit()
                await pending
        with closing(self.queue.connect()) as db:
            self.assertEqual(db.execute('SELECT chat_id FROM events').fetchall(), [(300,)])

    async def test_lock_timeout_returns_503_and_retry_accepts_whole_broadcast(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        request = SimpleNamespace(
            headers={'X-Tgbot-Secret': bot.SECRET}, app={bot.OUTBOX_KEY: self.queue},
            json=AsyncMock(return_value={'type': 'broadcast', 'chatIds': [200, 300],
                                        'text': 'retry', 'deliveryId': 'timeout-broadcast'}))
        connect = sqlite3.connect

        def short_timeout(*args, **kwargs):
            return connect(*args, **{**kwargs, 'timeout': 0.05})

        with closing(self.queue.connect()) as writer, writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute('UPDATE throttle SET until = 0')
            with patch('outbox.sqlite3.connect', side_effect=short_timeout):
                response = await bot.handle_event(request)
            self.assertEqual(response.status, 503)
            self.assertEqual(response.text, 'notification queue unavailable')
            self.assertEqual(writer.execute('SELECT count(*) FROM events').fetchone()[0], 0)
            self.assertEqual(writer.execute('SELECT count(*) FROM receipts').fetchone()[0], 0)
        for _ in range(2):
            self.assertEqual((await bot.handle_event(request)).status, 200)
        send = AsyncMock()
        await self.worker_until(send, lambda: not self.rows())
        self.assertCountEqual([call.kwargs['chat_id'] for call in send.await_args_list], [200, 300])

    def test_connection_is_closed_when_configuration_fails(self):
        db = Mock()
        db.execute.side_effect = sqlite3.OperationalError('database is locked')
        with patch('outbox.sqlite3.connect', return_value=db):
            with self.assertRaises(sqlite3.OperationalError):
                self.queue.connect()
        db.close.assert_called_once_with()

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

    async def test_permanent_rejection_is_reported_once_and_report_failure_is_harmless(self):
        reported = AsyncMock(side_effect=[RuntimeError('panel down'), None])
        for text in ('first', 'second'):
            self.queue.enqueue({'chat_id': 200, 'text': text})
            worker = asyncio.create_task(self.queue.run(AsyncMock(side_effect=TelegramSendError(403)), reported))
            try:
                async with asyncio.timeout(5):
                    while self.rows():
                        await asyncio.sleep(0.01)
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
        self.assertEqual(reported.await_args_list[0].args, ({'chat_id': 200, 'text': 'first'}, 403))
        self.assertEqual(reported.await_count, 2)
        # Повторяемые ошибки — не отказ.
        self.queue.enqueue({'chat_id': 200, 'text': 'retry'})
        await self.worker_until(AsyncMock(side_effect=TelegramSendError(503)), lambda: self.rows()[0][1] == 1)
        self.assertEqual(reported.await_count, 2)

    async def test_bot_reports_rejection_to_panel_journal(self):
        bot = load_bot()
        response = Mock(raise_for_status=Mock())
        with patch.object(bot._local, 'post', AsyncMock(return_value=response)) as post:
            await bot.report_rejected({'chat_id': bot.CHAT_ID, 'text': 'x'}, 400)
            await bot.report_rejected({'chat_id': bot.CHAT_ID + 1, 'text': 'x'}, 403)
        self.assertEqual([c.args[0] for c in post.await_args_list], ['/api/bot/events'] * 2)
        self.assertEqual([c.kwargs['json'] for c in post.await_args_list], [
            {'type': 'telegram_rejected', 'status': 400, 'recipient': 'admin'},
            {'type': 'telegram_rejected', 'status': 403, 'recipient': 'client'},
        ])
        self.assertTrue(post.await_args_list[0].kwargs['headers']['Authorization'].startswith('Bearer '))

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
