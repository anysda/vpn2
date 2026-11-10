"""Import the vpn2bot package with synthetic credentials."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def import_bot(extra_env=None):
    import importlib
    env = {
        'TELEGRAM_BOT_TOKEN': '123456:TEST_ONLY_TOKEN',
        'TELEGRAM_CHAT_ID': '100',
        'TGBOT_SECRET': 'TEST_ONLY_EVENT_SECRET',
        'TG_PROXY': 'http://127.0.0.1:9',
        **(extra_env or {}),
    }
    for name in [n for n in sys.modules if n == 'vpn2bot' or n.startswith('vpn2bot.')]:
        del sys.modules[name]
    with patch.dict(os.environ, env, clear=True), patch.object(Path, 'exists', return_value=False):
        package = importlib.import_module('vpn2bot')
        for sub in ('config', 'transport', 'panel', 'texts', 'client', 'admin',
                    'monitor', 'events', 'outbox', '__main__'):
            importlib.import_module(f'vpn2bot.{sub}')
    return package


def update(chat_id=200, user_id=200, chat_type='private', data=None, text='hello'):
    user = SimpleNamespace(id=user_id, username='test', is_bot=False)
    message = SimpleNamespace(text=text, sender_chat=None, message_id=1)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=user, effective_message=message, message=message,
        callback_query=(SimpleNamespace(id='callback', data=data, message=message)
                        if data is not None else None),
    )
