"""Load the actual bot with synthetic credentials, optionally from a Git revision."""
import os
from pathlib import Path
import subprocess
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


def load_bot(extra_env=None):
    path = Path(__file__).resolve().parents[1] / 'bot.py'
    revision = os.environ.get('BOT_REVISION')
    source = (subprocess.check_output(['git', 'show', f'{revision}:telegram/bot.py'], text=True)
              if revision else path.read_text())
    env = {
        'TELEGRAM_BOT_TOKEN': '123456:TEST_ONLY_TOKEN',
        'TELEGRAM_CHAT_ID': '100',
        'TGBOT_SECRET': 'TEST_ONLY_EVENT_SECRET',
        'TG_PROXY': 'http://127.0.0.1:9',
        **(extra_env or {}),
    }
    bot = ModuleType('audit_bot')
    with patch.dict(os.environ, env, clear=True), patch.object(Path, 'exists', return_value=False):
        exec(compile(source, str(path), 'exec'), bot.__dict__)
    return bot


def update(chat_id=200, user_id=200, chat_type='private', data=None, text='hello'):
    user = SimpleNamespace(id=user_id, username='test', is_bot=False)
    message = SimpleNamespace(text=text, sender_chat=None, message_id=1)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=user, effective_message=message, message=message,
        callback_query=(SimpleNamespace(id='callback', data=data, message=message)
                        if data is not None else None),
    )
