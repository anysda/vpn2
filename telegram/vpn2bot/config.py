import json
import logging
import os
import pathlib
import re


class _RedactTokenFormatter(logging.Formatter):
    """python-telegram-bot puts the token into the text of InvalidToken: cut secrets from every line."""
    secrets: tuple = ()

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for value in filter(None, self.secrets):
            text = text.replace(value, '<secret>')
        return re.sub(r'(https?://)[^/\s@]+@', r'\1<credentials>@', text)


logging.basicConfig(level=logging.INFO)
for _h in logging.getLogger().handlers:
    _h.setFormatter(_RedactTokenFormatter('%(asctime)s %(levelname)s %(name)s — %(message)s'))
# httpx logs every request URL at INFO, and the Bot API URL holds the token.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
log = logging.getLogger('tgbot')

# Written by the panel's «Боты»; overrides the environment, a change of it restarts the bot.
RUNTIME_PATH = pathlib.Path('/etc/anysda/telegram-runtime.json')
try:
    _runtime = json.loads(RUNTIME_PATH.read_text()) if RUNTIME_PATH.exists() else {}
except (OSError, ValueError):
    _runtime = {}
_runtime = _runtime if isinstance(_runtime, dict) else {}


def _chat_id() -> int:
    for source, raw in (('telegram-runtime.json', _runtime.get('chat_id')),
                        ('TELEGRAM_CHAT_ID', os.environ.get('TELEGRAM_CHAT_ID'))):
        raw = str(raw or '').strip()
        if re.fullmatch(r'-?[0-9]+', raw):
            return int(raw)
        if raw:
            log.error('chat_id %r из %s не число', raw, source)
    return 0


TOKEN = str(_runtime.get('bot_token') or os.environ.get('TELEGRAM_BOT_TOKEN', '')).strip()
CHAT_ID = _chat_id()
ADMIN_USERNAME = str(_runtime.get('admin_username') or os.environ.get('TELEGRAM_ADMIN_USERNAME', ''))
ADMIN_USERNAME = ADMIN_USERNAME.strip().lstrip('@').strip()
SECRET = os.environ.get('TGBOT_SECRET', '')
if not SECRET:
    raise RuntimeError('TGBOT_SECRET не задан — /event-эндпоинт отказался бы аутентифицировать. '
                       'Задай TGBOT_SECRET через env или /etc/anysda/telegram-runtime.json.')
_RedactTokenFormatter.secrets = (TOKEN, SECRET)
EVENT_PORT = int(os.environ.get('TGBOT_EVENT_PORT', '8877'))
OUTBOX_PATH = os.environ.get('TGBOT_OUTBOX_PATH', '/var/lib/anysda-tgbot/outbox.sqlite')
ANYSDA_URL = os.environ.get('ANYSDA_URL', 'http://127.0.0.1:51821')
# Telegram is reachable only through this sing-box proxy to the exit.
TG_PROXY = os.environ.get('TG_PROXY', 'http://127.0.0.1:7897')
# Seconds without a successful getUpdates before the process exits; 0 turns the watchdog off.
POLL_STALL_SEC = int(os.environ.get('TGBOT_POLL_STALL_SEC', '300'))
INVALID_TOKEN_PAUSE_SEC = int(os.environ.get('TGBOT_INVALID_TOKEN_PAUSE_SEC', '300'))
# Filled from getMe at start; invitation links need it.
BOT_USERNAME: str | None = None
