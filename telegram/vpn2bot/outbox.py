"""Panel notifications that survive a restart and a Telegram outage; one bot process owns the file."""
import asyncio
from contextlib import suppress
from datetime import timedelta
import hashlib
import json
import logging
from pathlib import Path
import random
import sqlite3
import time

from telegram.error import BadRequest, Forbidden, InvalidToken, RetryAfter, TelegramError

log = logging.getLogger('tgbot.outbox')

MAX_EVENTS = 1024
TTL_SEC = 24 * 60 * 60
LEASE_SEC = 120
# 8192 pages of 4 KiB bound the file at 32 MiB.
SCHEMA = '''PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL; PRAGMA max_page_count=8192;
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
    payload TEXT NOT NULL, expires REAL NOT NULL, due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS receipts (delivery_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
    event_id INTEGER NOT NULL, expires REAL NOT NULL);'''


# Telegram refused the message for good: dropped and reported to the panel with this status.
REJECTED = {BadRequest: 400, Forbidden: 403, InvalidToken: 404}
# Not sent within its lifetime: dropped and reported like a refusal, so a queued message is never lost silently.
EXPIRED = 504


class QueueFull(Exception):
    pass


class Outbox:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.executescript(SCHEMA)
        Path(path).chmod(0o600)
        self.throttled_until = 0.0
        self.wake = asyncio.Event()
        # Outcome of a message somebody waits for: None - sent, a status - dropped (REJECTED, EXPIRED).
        self.waiting: dict[int, asyncio.Future] = {}
        self.expired: list[dict] = []

    def settle(self, event_id: int, status: int | None) -> None:
        future = self.waiting.pop(event_id, None)
        if future and not future.done():
            future.set_result(status)

    def expire(self, now: float) -> None:
        rows = self.db.execute('DELETE FROM events WHERE expires <= ? RETURNING id, payload', (now,)).fetchall()
        self.db.execute('DELETE FROM receipts WHERE expires <= ?', (now,))
        if rows:
            log.warning('outbox expired=%d', len(rows))
        for event_id, payload in rows:
            self.expired.append(json.loads(payload))
            self.settle(event_id, EXPIRED)

    def enqueue(self, payloads: list[dict], delivery_id: str | None = None, ttl: float = TTL_SEC) -> list[int]:
        """All payloads or none; a repeated delivery_id with the same payloads returns the first ids."""
        if not payloads:
            raise ValueError('no messages')
        for payload in payloads:
            text = payload['caption'] if 'file' in payload else payload['text']
            if not isinstance(text, str) or not text.strip() or len(text.encode('utf-16-le')) > 8192:
                raise ValueError('invalid notification text')
        if delivery_id is not None and (not isinstance(delivery_id, str) or not 1 <= len(delivery_id) <= 128):
            raise ValueError('invalid delivery id')
        digest = hashlib.sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()
        now = time.time()
        with self.db:
            self.expire(now)
            receipt = self.db.execute('SELECT digest, event_id FROM receipts WHERE delivery_id = ?',
                                      (delivery_id,)).fetchone()
            if receipt:
                if receipt[0] != digest:
                    raise ValueError('delivery id reused with different payload')
                return [receipt[1]]
            if self.db.execute('SELECT count(*) FROM events').fetchone()[0] + len(payloads) > MAX_EVENTS:
                raise QueueFull()
            ids = [self.db.execute(
                'INSERT INTO events (chat_id, payload, expires, due) VALUES (?, ?, ?, ?)',
                (payload['chat_id'], json.dumps(payload | ({'delivery_id': delivery_id} if delivery_id else {}), ensure_ascii=False), now + ttl,
                 max(now, self.throttled_until))).lastrowid for payload in payloads]
            if delivery_id is not None:
                self.db.execute('INSERT INTO receipts VALUES (?, ?, ?, ?)',
                                (delivery_id, digest, ids[-1], now + TTL_SEC))
        log.info('outbox accepted id=%d', ids[-1])
        self.wake.set()
        return ids

    async def outcome(self, ids: list[int], timeout: float) -> list[int | None] | None:
        """What became of the messages within `timeout`; None while any of them is still queued."""
        loop = asyncio.get_running_loop()
        futures = [self.waiting.setdefault(event_id, loop.create_future()) for event_id in ids]
        try:
            done, _ = await asyncio.wait(futures, timeout=timeout)
        finally:
            for event_id in ids:
                self.waiting.pop(event_id, None)
        return [future.result() for future in futures] if len(done) == len(futures) else None

    def claim(self) -> dict | None:
        """The oldest due message of a chat whose earlier messages are all gone; leased before the send."""
        now = time.time()
        if self.throttled_until > now:
            return None
        with self.db:
            self.expire(now)
            row = self.db.execute('''SELECT id, payload, attempts FROM events e WHERE due <= ? AND NOT EXISTS
                (SELECT 1 FROM events older WHERE older.chat_id = e.chat_id AND older.id < e.id)
                ORDER BY id LIMIT 1''', (now,)).fetchone()
            if row is None:
                return None
            self.db.execute('UPDATE events SET due = ?, attempts = attempts + 1 WHERE id = ?',
                            (now + LEASE_SEC, row[0]))
        return dict(zip(('id', 'payload', 'attempts'), row))

    def remove(self, event_id: int) -> None:
        with self.db:
            self.db.execute('DELETE FROM events WHERE id = ?', (event_id,))

    async def run(self, send, on_rejected) -> None:
        while True:
            try:
                self.wake.clear()
                row = self.claim()
                while self.expired:
                    await self.report(on_rejected, self.expired.pop(0), EXPIRED)
                if row is None:
                    with suppress(TimeoutError):
                        async with asyncio.timeout(0.5):
                            await self.wake.wait()
                    continue
                await self.deliver(row, send, on_rejected)
            except (sqlite3.Error, OSError):
                log.error('outbox storage unavailable')
                await asyncio.sleep(1)

    async def deliver(self, row: dict, send, on_rejected) -> None:
        payload = json.loads(row['payload'])
        # The panel's delivery this message belongs to: its bell row shows the refusal.
        delivery_id = payload.pop('delivery_id', None)
        rejected = None
        try:
            async with asyncio.timeout(45):
                await send(**payload)
            log.info('outbox delivered id=%d', row['id'])
        except Exception as error:
            rejected = next((status for kind, status in REJECTED.items() if isinstance(error, kind)), None)
            if rejected is None:
                delay = min(300, 5 * 2 ** min(row['attempts'], 6) * random.uniform(1, 1.2))
                if isinstance(error, RetryAfter):
                    wait = error.retry_after
                    delay = max(delay, wait.total_seconds() if isinstance(wait, timedelta) else wait)
                    self.throttled_until = max(self.throttled_until, time.time() + delay)
                elif not isinstance(error, TelegramError) or error.__cause__ is not None:
                    # No answer from Telegram (timeout, lost connection, broken body): the outcome is unknown,
                    # and the Bot API has no idempotency key, so such a send waits at least a minute.
                    delay = max(delay, 60)
                with self.db:
                    self.db.execute('UPDATE events SET due = ? WHERE id = ?', (time.time() + delay, row['id']))
                log.warning('outbox retry id=%d attempt=%d delay=%.1fs error=%s',
                            row['id'], row['attempts'] + 1, delay, type(error).__name__)
                return
            log.warning('outbox rejected id=%d status=%d', row['id'], rejected)
            await self.report(on_rejected, {**payload, 'delivery_id': delivery_id}, rejected)
        # A sent message is not sent again because its acknowledgement failed to commit.
        while True:
            try:
                self.remove(row['id'])
                break
            except sqlite3.Error:
                log.error('outbox acknowledgement failed id=%d', row['id'])
                await asyncio.sleep(1)
        self.settle(row['id'], rejected)

    @staticmethod
    async def report(on_rejected, payload: dict, status: int) -> None:
        try:
            await on_rejected(payload, status, payload.pop('delivery_id', None))
        except Exception as error:
            log.warning('outbox rejection report failed status=%d error=%s', status, type(error).__name__)
