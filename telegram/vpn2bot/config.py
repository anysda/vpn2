import json
import logging
import os
import pathlib
import re


class _RedactTokenFormatter(logging.Formatter):
    """Вырезает токен бота из любой строки лога, включая трассировки:
    python-telegram-bot кладёт токен прямо в текст InvalidToken."""
    secrets: tuple = ()

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for value in filter(None, self.secrets):
            text = text.replace(value, '<secret>')
        return re.sub(r'(https?://)[^/\s@]+@', r'\1<credentials>@', text)


logging.basicConfig(level=logging.INFO)
for _h in logging.getLogger().handlers:
    _h.setFormatter(_RedactTokenFormatter('%(asctime)s %(levelname)s %(name)s — %(message)s'))
# httpx на INFO пишет URL каждого запроса, а в URL Telegram API сидит токен.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
log = logging.getLogger('tgbot')

# Настройки из панели («Боты») перебивают env: панель пишет файл, бот по смене
# mtime выходит, docker --restart поднимает его с новыми значениями.
RUNTIME_PATH = pathlib.Path('/etc/anysda/telegram-runtime.json')


def _load_runtime() -> tuple[str, int, str]:
    """Токен, Chat ID и никнейм админа без @ (он в ответе непривязанным людям)."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    env_chat = os.environ.get('TELEGRAM_CHAT_ID', '0').strip()
    chat = ''
    admin = os.environ.get('TELEGRAM_ADMIN_USERNAME', '')
    if RUNTIME_PATH.exists():
        try:
            data = json.loads(RUNTIME_PATH.read_text())
            token = (data.get('bot_token') or token).strip()
            chat = str(data.get('chat_id') or '').strip()
            admin = (data.get('admin_username') or admin or '').strip()
        except Exception:
            pass
    admin = admin.lstrip('@').strip()
    # Кривой chat_id (например «@mychannel») не должен ронять бота по кругу:
    # откат на env; если и там не число — 0, дальше та же ошибка, что и без env.
    for source, raw in (('telegram-runtime.json', chat), ('TELEGRAM_CHAT_ID', env_chat)):
        if re.fullmatch(r'-?[0-9]+', raw):
            return token, int(raw), admin
        if raw:
            log.error('chat_id %r из %s не число', raw, source)
    return token, 0, admin


TOKEN, CHAT_ID, ADMIN_USERNAME = _load_runtime()
SECRET = os.environ.get('TGBOT_SECRET', '')
if not SECRET:
    raise RuntimeError(
        'TGBOT_SECRET не задан — /event-эндпоинт отказался бы аутентифицировать. '
        'Задай TGBOT_SECRET через env или /etc/anysda/telegram-runtime.json.'
    )
_RedactTokenFormatter.secrets = (TOKEN, SECRET)
# Порт, на котором панель шлёт события боту и docker спрашивает /health.
EVENT_PORT = int(os.environ.get('TGBOT_EVENT_PORT', '8877'))
OUTBOX_PATH = os.environ.get('TGBOT_OUTBOX_PATH', '/var/lib/anysda-tgbot/outbox.sqlite')
ANYSDA_URL = os.environ.get('ANYSDA_URL', 'http://127.0.0.1:51821')
# Локальный HTTP-прокси sing-box до экзита: Telegram доступен только через него.
TG_PROXY = os.environ.get('TG_PROXY', 'http://127.0.0.1:7897')
# Сколько секунд без успешного getUpdates считать зависанием поллера (см.
# api.poll_watchdog). Обычный long-poll отвечает за ~10 с. 0 = сторож выключен.
POLL_STALL_SEC = int(os.environ.get('TGBOT_POLL_STALL_SEC', '300'))
# Отвергнутый токен сам не починится: сколько ждать перед выходом под рестарт.
INVALID_TOKEN_PAUSE_SEC = int(os.environ.get('TGBOT_INVALID_TOKEN_PAUSE_SEC', '300'))

# username бота — заполняется при старте через getMe; нужен для диплинков-приглашений.
BOT_USERNAME: str | None = None
