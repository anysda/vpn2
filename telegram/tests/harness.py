"""Run the bot against a fake panel and a fake Telegram and record every call.

The record of a scenario is everything the bot sent out: Telegram methods with
their payloads, panel requests, answers to /event and queued notifications.
"""
import asyncio
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODULES = ('config', 'texts', 'api', 'outbox', 'handlers', 'monitor', 'events', '__main__')
ENV = {
    'TELEGRAM_BOT_TOKEN': '123456:TEST_ONLY_TOKEN',
    'TELEGRAM_CHAT_ID': '100',
    'TGBOT_SECRET': 'TEST_ONLY_EVENT_SECRET',
    'TG_PROXY': 'http://127.0.0.1:9',
}
# who writes: chat id, user id, chat type
WHO = {
    'admin': (100, 100, 'private'),
    'client': (200, 200, 'private'),
    'client_in_group': (300, 200, 'group'),
    'group_admin': (-100500, 7, 'supergroup'),
    'group_member': (-100500, 8, 'supergroup'),
    'group_anonymous': (-100500, 9, 'supergroup'),
    'group_bot': (-100500, 10, 'supergroup'),
}


def import_bot(extra_env=None):
    """Import a fresh vpn2bot with synthetic credentials and no runtime file."""
    for name in [n for n in sys.modules if n == 'vpn2bot' or n.startswith('vpn2bot.')]:
        del sys.modules[name]
    env = {**ENV, **(extra_env or {})}
    with patch.dict(os.environ, env, clear=True), patch.object(Path, 'exists', return_value=False):
        package = importlib.import_module('vpn2bot')
        for sub in MODULES:
            importlib.import_module(f'vpn2bot.{sub}')
    return package


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body
        self.content = body
        self.text = body if isinstance(body, str) else ''

    def json(self):
        if isinstance(self.body, (dict, list)):
            return self.body
        raise ValueError('not json')

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


class FakeHttp:
    """Stands in for an httpx.AsyncClient: records the request, answers from a table."""

    def __init__(self, kind, record, answers, default):
        self.kind, self.record, self.answers, self.default = kind, record, answers, default

    async def request(self, method, path, *, json=None, data=None, files=None, **_):
        entry = [self.kind, method, path, json if json is not None else data]
        for field, (filename, content, _mime) in (files or {}).items():
            entry.append([field, filename, content.decode('utf-8', 'backslashreplace')])
        self.record.append(entry)
        answer = self.answers.get(f'{method} {path}', self.default)
        if isinstance(answer, list):
            answer = answer.pop(0) if answer else self.default
        if isinstance(answer, Exception):
            raise answer
        return Response(*answer)

    async def get(self, path, **kwargs):
        return await self.request('GET', path, **kwargs)

    async def post(self, path, **kwargs):
        return await self.request('POST', path, **kwargs)


def update(who, what):
    """A Telegram update: text (or a command) from a chat, or ('press', data)."""
    chat_id, user_id, chat_type = WHO[who]
    press = what[1] if isinstance(what, tuple) else None
    message = SimpleNamespace(text=None if press is not None else what, message_id=1,
                              sender_chat=object() if who == 'group_anonymous' else None)
    query = None if press is None else SimpleNamespace(id='callback', data=press, message=message)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=SimpleNamespace(id=user_id, username='test', is_bot=who == 'group_bot'),
        effective_message=message, message=message, callback_query=query)


def wire(bot, telegram, panel):
    bot.api.telegram_http, bot.api.panel_http = telegram, panel


async def deliver(bot, incoming):
    await bot.handlers.dispatch(incoming, None)


async def post_event(bot, outbox, body, trusted):
    """POST /event from the panel; a str body stands for a broken JSON."""
    async def read_json():
        if isinstance(body, str):
            raise ValueError('not json')
        return body

    request = SimpleNamespace(
        headers={'X-Tgbot-Secret': ENV['TGBOT_SECRET'] if trusted else 'wrong'},
        app={bot.events.OUTBOX_KEY: outbox}, json=read_json)
    response = await bot.events.handle_event(request)
    return [response.status, response.text]


def queued(outbox):
    """Take everything the bot has put into the notification queue."""
    payloads = []
    while (row := outbox.claim()) is not None:
        payloads.append(json.loads(row['payload']))
        outbox.remove(row['id'])
    return payloads


async def play(scenario, defaults):
    """Play the steps of a scenario; return the record of what the bot did."""
    record = []
    bot = import_bot(scenario.get('env'))
    bot.config.BOT_USERNAME = scenario.get('bot_username', 'vpn_test_bot')
    panel = copy.deepcopy({**defaults, **scenario.get('panel', {})})
    telegram = {f'POST /{method}': answer
                for method, answer in copy.deepcopy(scenario.get('tg', {})).items()}
    wire(bot, FakeHttp('tg', record, telegram, (200, {'ok': True, 'result': {}})),
         FakeHttp('panel', record, panel, (404, {'statusMessage': 'not_found'})))
    clock = SimpleNamespace(now=1000.0, feed=None)
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        clock.now += seconds
        if clock.feed is not None and not clock.feed:
            raise asyncio.CancelledError
        await real_sleep(0)

    with tempfile.TemporaryDirectory() as tmp, patch.object(asyncio, 'sleep', fake_sleep):
        outbox = bot.outbox.Outbox(f'{tmp}/outbox.sqlite')
        for who, what in scenario['steps']:
            if who in ('panel', 'intruder'):
                record.append(['event', *await post_event(bot, outbox, what, who == 'panel')])
            elif who == 'monitor':
                clock.feed = panel['GET /api/ops/bot-snapshot'] = list(what)
                try:
                    await bot.monitor.monitor_loop()
                except asyncio.CancelledError:
                    clock.feed = None
            else:
                await deliver(bot, update(who, what))
            # Background sends the bot started for this step.
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            await asyncio.gather(*others, return_exceptions=True)
            record.extend(['queued', payload] for payload in queued(outbox))
        outbox.db.close()
    return record
