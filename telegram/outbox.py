"""Bounded, crash-safe outbox for panel notifications (one bot process)."""
import asyncio
from contextlib import closing
import hashlib
import json
import logging
from pathlib import Path
import random
import sqlite3
import time

log = logging.getLogger('tgbot.outbox')

# At most ~16 MiB of text, 32 MiB of SQLite pages plus a bounded rollback
# journal. Suitable for a small entry node; no dependency on available RAM.
MAX_EVENTS = 1024
TTL_SEC = 24 * 60 * 60
MAX_TEXT_BYTES = 16 * 1024
LEASE_SEC = 120
MAX_RECEIPTS = 32768


class QueueFull(Exception):
    pass


class TelegramSendError(RuntimeError):
    def __init__(self, status: int, retry_after: float = 0):
        super().__init__(f'Telegram send failed (HTTP {status})')
        self.status = status
        self.retry_after = retry_after


class Outbox:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('''CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                payload TEXT NOT NULL, expires REAL NOT NULL,
                due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)''')
            db.execute('''CREATE TABLE IF NOT EXISTS throttle (
                id INTEGER PRIMARY KEY CHECK (id = 1), until REAL NOT NULL)''')
            db.execute('INSERT OR IGNORE INTO throttle VALUES (1, 0)')
            db.execute('''CREATE TABLE IF NOT EXISTS receipts (
                delivery_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                event_id INTEGER NOT NULL, expires REAL NOT NULL)''')
        Path(path).chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.2)
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA max_page_count=8192')
        return db

    @staticmethod
    def expire(db, now):
        count = db.execute('DELETE FROM events WHERE expires <= ?', (now,)).rowcount
        if count:
            log.warning('outbox expired=%d', count)

    @staticmethod
    def validate(payload):
        text = payload['text']
        if (not isinstance(text, str) or not text.strip()
                or len(text.encode('utf-8')) > MAX_TEXT_BYTES
                or len(text.encode('utf-16-le')) // 2 > 4096):
            raise ValueError('invalid notification text')

    def enqueue(self, payload: dict, delivery_id=None) -> int:
        return self.enqueue_batch([payload], delivery_id)

    def enqueue_batch(self, payloads, delivery_id=None) -> int:
        if not payloads or len(payloads) > MAX_EVENTS:
            raise QueueFull()
        for payload in payloads:
            self.validate(payload)
        if delivery_id is not None and (not isinstance(delivery_id, str)
                                       or not 1 <= len(delivery_id) <= 128):
            raise ValueError('invalid delivery id')
        digest = hashlib.sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db, now)
            db.execute('DELETE FROM receipts WHERE expires <= ?', (now,))
            if delivery_id is not None:
                receipt = db.execute('SELECT digest, event_id FROM receipts WHERE delivery_id = ?',
                                     (delivery_id,)).fetchone()
                if receipt:
                    if receipt[0] != digest:
                        raise ValueError('delivery id reused with different payload')
                    return receipt[1]
                if db.execute('SELECT count(*) FROM receipts').fetchone()[0] >= MAX_RECEIPTS:
                    raise QueueFull()
            if db.execute('SELECT count(*) FROM events').fetchone()[0] + len(payloads) > MAX_EVENTS:
                raise QueueFull()
            due = max(now, db.execute('SELECT until FROM throttle').fetchone()[0])
            for payload in payloads:
                event_id = db.execute(
                    'INSERT INTO events (chat_id, payload, expires, due) VALUES (?, ?, ?, ?)',
                    (payload['chat_id'], json.dumps(payload, ensure_ascii=False), now + TTL_SEC, due),
                ).lastrowid
            if delivery_id is not None:
                db.execute('INSERT INTO receipts VALUES (?, ?, ?, ?)',
                           (delivery_id, digest, event_id, now + TTL_SEC))
        log.info('outbox accepted id=%d', event_id)
        return event_id

    def claim(self):
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db, now)
            if db.execute('SELECT until FROM throttle').fetchone()[0] > now:
                return None
            # Keep order within each chat, without blocking other recipients.
            row = db.execute('''SELECT id, payload, attempts FROM events e WHERE due <= ? AND NOT EXISTS
                (SELECT 1 FROM events older WHERE older.chat_id = e.chat_id AND older.id < e.id)
                ORDER BY id LIMIT 1''', (now,)).fetchone()
            if row:
                row = dict(zip(('id', 'payload', 'attempts'), row))
                # Persist the lease BEFORE the request: a killed process cannot
                # immediately resend a message with an uncertain outcome.
                db.execute('UPDATE events SET due = ?, attempts = attempts + 1 WHERE id = ?',
                           (now + LEASE_SEC, row['id']))
            return row

    def remove(self, event_id):
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM events WHERE id = ?', (event_id,))

    def retry(self, event_id, delay, rate_limited):
        until = time.time() + delay
        with closing(self.connect()) as db, db:
            db.execute('UPDATE events SET due = ? WHERE id = ?', (until, event_id))
            if rate_limited:
                db.execute('UPDATE throttle SET until = max(until, ?)', (until,))

    async def run(self, send):
        while True:
            try:
                row = await asyncio.to_thread(self.claim)
                if row is None:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    # Less than the lease, even if multiple HTTP phases stall.
                    async with asyncio.timeout(45):
                        await send(**json.loads(row['payload']))
                except Exception as error:
                    permanent = isinstance(error, TelegramSendError) and error.status in (400, 403, 404)
                    if not permanent:
                        delay = min(300, 5 * 2 ** min(row['attempts'], 6) * random.uniform(1, 1.2))
                        # Bot API has no sendMessage idempotency key. Retrying
                        # an ambiguous send can duplicate it; prefer delivery,
                        # with a cooldown rather than an immediate blind retry.
                        if not isinstance(error, TelegramSendError):
                            delay = max(60, delay)
                        limited = isinstance(error, TelegramSendError) and error.status == 429
                        if limited:
                            delay = max(delay, error.retry_after)
                        await asyncio.to_thread(self.retry, row['id'], delay, limited)
                        log.warning('outbox retry id=%d attempt=%d delay=%.1fs error=%s',
                                    row['id'], row['attempts'] + 1, delay, type(error).__name__)
                        continue
                    log.warning('outbox rejected id=%d status=%d', row['id'], error.status)
                else:
                    log.info('outbox delivered id=%d', row['id'])
                # A successful send must not be repeated just because the ACK
                # commit failed. Keep retrying only the local delete until saved.
                while True:
                    try:
                        await asyncio.to_thread(self.remove, row['id'])
                        break
                    except sqlite3.Error:
                        log.error('outbox acknowledgement failed id=%d', row['id'])
                        await asyncio.sleep(1)
            except (sqlite3.Error, OSError):
                log.error('outbox storage unavailable')
                await asyncio.sleep(1)
