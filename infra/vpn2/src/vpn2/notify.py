"""Доставка событий: локальный бот, панель и Telegram Bot API напрямую.

Секрет и токен никогда не попадают в причину отказа, в текст исключения
и в результат: наружу идут только фиксированные тексты.
"""
import enum
import json
import math
import uuid
from http.client import HTTPException
from urllib import error, parse, request

BOT_URL = 'http://127.0.0.1:8877/event'
PANEL_URL = 'http://127.0.0.1:51821/api/bot/events'
SECRET_FILE = '/etc/anysda/tgbot-secret.txt'

# Telegram refuses these for good (deleted chat, blocked bot, malformed
# request): repeating them only spams the API, so the row ends with the reason.
PERMANENT = {400, 403}
# Fixed texts for known descriptions; the API wording itself is never stored.
DESCRIPTIONS = {'chat not found': 'чат не найден',
                'bot was blocked by the user': 'бот заблокирован пользователем',
                'user is deactivated': 'пользователь удалён',
                "bot can't initiate conversation with a user": 'пользователь не запускал бота'}


class Outcome(enum.Enum):
    """Итог одной попытки доставки."""
    SENT = 'sent'
    NO_BOT = 'no_bot'
    FAILED = 'failed'


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa - urllib callback
        del req, fp, code, msg, headers, newurl
        return None


# Один на процесс: opener на каждый вызов растит процесс навсегда.
_OPENER = request.build_opener(_NoRedirect())


def utf16_len(text):
    """Длина строки в единицах UTF-16."""
    return len(text.encode('utf-16-le', 'surrogatepass')) // 2


def clean_detail(text, limit=200):
    """Чужой вывод -> безопасная строка: UTF-8 с заменой, непечатное -> пробел,
    пробелы схлопнуты, не больше limit знаков и limit единиц UTF-16."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode('utf-8', 'replace')
    elif not isinstance(text, str):
        text = str(text)
    text = text.encode('utf-8', 'replace').decode('utf-8', 'replace')
    text = ' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())[:limit]
    while utf16_len(text) > limit:
        text = text[:-1]
    return text.strip()


def _read_secret(secret_file):
    try:
        with open(secret_file, encoding='utf-8') as handle:
            return handle.read().strip()
    except (OSError, ValueError):
        return ''


def _post(event, url, headers, timeout, opener, confirm):
    body = json.dumps(event).encode()
    req = request.Request(url, data=body, headers={'Content-Type': 'application/json', **headers})
    try:
        with (opener or _OPENER).open(req, timeout=timeout) as response:
            status = getattr(response, 'status', None) or getattr(response, 'code', 200)
            if not 200 <= status < 300:
                return Outcome.FAILED
            if confirm is None:
                return Outcome.SENT
            result = json.load(response)
    except (OSError, HTTPException, ValueError, TypeError, AttributeError):
        return Outcome.FAILED
    if confirm and not (isinstance(result, dict) and result.get('ok') and 'event_id' in result):
        return Outcome.FAILED
    return Outcome.SENT


def bot_event(event, *, secret_file=SECRET_FILE, url=BOT_URL, timeout=5, confirm=True,
              opener=None):
    """Одна попытка отправить событие локальному боту.

    Нет или пустой секрет -> NO_BOT без запроса. confirm=True: SENT только при
    {'ok': true, 'event_id'}; False: любой 2xx с JSON. Не бросает."""
    secret = _read_secret(secret_file)
    if not secret:
        return Outcome.NO_BOT
    return _post(event, url, {'X-Tgbot-Secret': secret}, timeout, opener, bool(confirm))


def flush(pending, **kwargs):
    """Шлёт очередь по порядку, снимает с головы только доставленное; на первом
    не-SENT стоп. Список меняется на месте. Пустая очередь -> SENT."""
    while pending:
        outcome = bot_event(pending[0], **kwargs)
        if outcome is not Outcome.SENT:
            return outcome
        pending.pop(0)
    return Outcome.SENT


def panel_event(event, *, secret_file=SECRET_FILE, url=PANEL_URL, timeout=5, opener=None):
    """Событие журналу панели: Bearer тем же секретом, SENT на любой 2xx."""
    secret = _read_secret(secret_file)
    if not secret:
        return Outcome.NO_BOT
    return _post(event, url, {'Authorization': f'Bearer {secret}'}, timeout, opener, None)


class Reply:
    """Итог одного запроса к Bot API."""
    __slots__ = ('ok', 'reason', 'retry_after', 'final', 'result')

    def __init__(self, ok, reason='', retry_after=0, final=False, result=None):
        self.ok = ok
        self.reason = reason
        self.retry_after = retry_after
        self.final = final
        self.result = result

    def __iter__(self):
        return iter((self.ok, self.reason, self.retry_after, self.final, self.result))

    def __repr__(self):
        return (f'Reply(ok={self.ok!r}, reason={self.reason!r}, '
                f'retry_after={self.retry_after!r}, final={self.final!r})')


class BotApi:
    """Клиент Telegram Bot API. Токен живёт только в адресе запроса."""

    def __init__(self, token, *, api_base='https://api.telegram.org', timeout=5, opener=None):
        endpoint = parse.urlsplit(api_base)
        if (endpoint.scheme != 'https'
                and not (endpoint.scheme == 'http'
                         and endpoint.hostname in {'127.0.0.1', '::1'})):
            raise ValueError('HTTPS Bot API required')
        self._token = token
        self._api_base = api_base
        self._timeout = timeout
        self._opener = opener or _OPENER

    def __repr__(self):
        return 'BotApi()'

    def _url(self, method):
        return f'{self._api_base.rstrip("/")}/bot{parse.quote(self._token, safe=":")}/{method}'

    def call(self, chat, part):
        """Один запрос; не бросает на сети и ответе, причина без токена и сырого ответа."""
        method = part['method']
        if method in ('sendMessage', 'editMessageText'):
            fields = {key: part[key] for key in ('message_id', 'reply_markup') if key in part}
            body = json.dumps({'chat_id': chat, 'text': part['text'],
                               'link_preview_options': {'is_disabled': True},
                               **fields}).encode()
            content_type = 'application/json'
        else:
            boundary = uuid.uuid4().hex
            fields = {'chat_id': str(chat), 'caption': part['caption']}
            body = b''
            for key, value in fields.items():
                body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                         f'\r\n\r\n{value}\r\n').encode()
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                     f'filename="{part["filename"]}"\r\nContent-Type: text/plain; '
                     'charset=utf-8\r\n\r\n').encode()
            body += part['document'].encode() + f'\r\n--{boundary}--\r\n'.encode()
            content_type = f'multipart/form-data; boundary={boundary}'
        req = request.Request(self._url(method), data=body,
                              headers={'Content-Type': content_type})
        try:
            try:
                response = self._opener.open(req, timeout=self._timeout)
            except error.HTTPError as exc:
                response = exc
            with response:
                status = response.code
                data = json.loads(response.read(65536))
            if status == 200 and data.get('ok') is True:
                return Reply(True, '', 0, False, data.get('result'))
            code = data.get('error_code', status)
            if code == 429:
                delay = float(data.get('parameters', {}).get('retry_after', 1))
                if not math.isfinite(delay) or delay < 0:
                    delay = 1
                return Reply(False, 'Telegram 429', max(1, delay), False, None)
            if not isinstance(code, int):
                return Reply(False, 'ошибка Telegram', 0, False, None)
            description = str(data.get('description', '')).lower()
            known = next((text for key, text in DESCRIPTIONS.items() if key in description), None)
            return Reply(False, f'Telegram {code}: {known}' if known else f'Telegram {code}',
                         0, code in PERMANENT, None)
        except (OSError, HTTPException, ValueError, TypeError, AttributeError):
            return Reply(False, 'Telegram недоступен или ответ не подтверждён', 0, False, None)

    def reachable(self):
        """Отвечает ли Bot API хоть как-то (getMe); любой HTTP-ответ = True."""
        req = request.Request(self._url('getMe'))
        try:
            with self._opener.open(req, timeout=self._timeout):
                return True
        except error.HTTPError as exc:
            exc.close()
            return True
        except (OSError, HTTPException, ValueError):
            return False
