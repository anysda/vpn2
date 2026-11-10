import json
import logging
import os
import pathlib
import re


class _RedactTokenFormatter(logging.Formatter):
    """Вырезает токен бота из любой строки лога, включая трассировки:
    python-telegram-bot кладёт токен прямо в текст InvalidToken."""
    token = ''
    secret = ''

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for value in (self.token, self.secret):
            if value:
                text = text.replace(value, '<secret>')
        return re.sub(r'(https?://)[^/\s@]+@', r'\1<credentials>@', text)


logging.basicConfig(level=logging.INFO)
for _h in logging.getLogger().handlers:
    _h.setFormatter(_RedactTokenFormatter('%(asctime)s %(levelname)s %(name)s — %(message)s'))
# httpx на INFO пишет URL каждого запроса, а в URL Telegram API сидит токен.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
log = logging.getLogger('tgbot')

# Runtime config поверх env-vars: если файл существует — он перебивает токен/чат_id.
# Используется панелью (Боты): при сохранении в UI Nuxt пишет сюда, бот сам
# делает os._exit(0) при изменении mtime, docker --restart always
# перезапускает контейнер с новыми значениями.
RUNTIME_PATH = pathlib.Path('/etc/anysda/telegram-runtime.json')


def _load_runtime() -> tuple[str, int, str]:
    """Прочитать bot_token / chat_id / admin_username (env + runtime.json).

    runtime.json перебивает env. admin_username — никнейм админа без @,
    используется в сообщении непривязанным людям («напишите администратору»).
    """
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
        if not raw:
            continue
        if re.fullmatch(r'-?[0-9]+', raw):
            return token, int(raw), admin
        log.error('chat_id %r из %s не число', raw, source)
    return token, 0, admin


TOKEN, CHAT_ID, ADMIN_USERNAME = _load_runtime()
_RedactTokenFormatter.token = TOKEN
SECRET     = os.environ.get('TGBOT_SECRET', '')
_RedactTokenFormatter.secret = SECRET
if not SECRET:
    raise RuntimeError(
        'TGBOT_SECRET не задан — /event-эндпоинт отказался бы аутентифицировать. '
        'Задай TGBOT_SECRET через env или /etc/anysda/telegram-runtime.json.'
    )
EVENT_PORT = int(os.environ.get('TGBOT_EVENT_PORT', '8877'))
ANYSDA_URL = os.environ.get('ANYSDA_URL', 'http://127.0.0.1:51821')
# Telegram API egress: RU-нода в Москве api.telegram.org напрямую не достаёт,
# поэтому Telegram-клиенты бота ходят через этот локальный HTTP-прокси
# (sing-box mixed-инбаунд → foreign-best → экзит). Обращения к Nuxt API на
# 127.0.0.1 проксировать НЕ нужно — _local идёт напрямую.
TG_PROXY   = os.environ.get('TG_PROXY', 'http://127.0.0.1:7897')
# Сторож поллера: сколько секунд без успешного getUpdates считать зависанием.
# 03-08-2026 после флапа экзитов sing-box порвал CONNECT-тоннели, сокеты бота
# остались в CLOSE-WAIT, а поллер молча встал: процесс жив, monitor_loop тикает,
# апдейты из Telegram не забираются ~38 часов и НИ ОДНОЙ ошибки в логе. Лечится
# только рестартом, поэтому сторож роняет процесс — docker поднимет заново.
# Нормальный интервал long-poll ≈ 10с, так что 300с — заведомо аномалия. 0 = выкл.
POLL_STALL_SEC = int(os.environ.get('TGBOT_POLL_STALL_SEC', '300'))
# Отвергнутый токен сам не починится: сколько ждать перед выходом под рестарт.
INVALID_TOKEN_PAUSE_SEC = int(os.environ.get('TGBOT_INVALID_TOKEN_PAUSE_SEC', '300'))

# username бота — заполняется в main() через getMe; нужен для диплинков-приглашений.
BOT_USERNAME: str | None = None
