"""Доставка событий: бот, панель, Bot API. Сеть подменена opener-ом."""
import io
import json
import socket
from urllib import error

import pytest

from vpn2 import notify
from vpn2.notify import BotApi, Outcome

SECRET = 'TEST_ONLY_SECRET'
TOKEN = '123456:TEST-TOKEN-abcdef'
EVENT = {'type': 'system_alert', 'subject': 'Маршрут YouTube', 'detail': 'узел недоступен'}


class Resp:
    def __init__(self, status=200, body=b'{}'):
        self.code = self.status = status
        self._body = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def close(self):
        pass


class Opener:
    """Отвечает по очереди; Exception в очереди бросается."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def http_error(code, body):
    return error.HTTPError('http://x', code, 'm', {}, io.BytesIO(json.dumps(body).encode()))


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / 'secret.txt'
    path.write_text(SECRET + '\n')
    return str(path)


def ok_bot(n=1):
    return Resp(200, {'ok': True, 'event_id': n})


def test_константы():
    assert notify.BOT_URL == 'http://127.0.0.1:8877/event'
    assert notify.PANEL_URL == 'http://127.0.0.1:51821/api/bot/events'
    assert notify.SECRET_FILE == '/etc/anysda/tgbot-secret.txt'


def test_bot_event_отправляет_секрет_и_тело(secret_file):
    op = Opener(ok_bot())
    assert notify.bot_event(EVENT, secret_file=secret_file, opener=op) is Outcome.SENT
    req, timeout = op.requests[0]
    assert req.get_header('X-tgbot-secret') == SECRET
    assert json.loads(req.data) == EVENT
    assert req.full_url == notify.BOT_URL and timeout == 5


@pytest.mark.parametrize('content', [None, '', '\n', '  \n'])
def test_bot_event_нет_секрета(tmp_path, content):
    path = tmp_path / 's'
    if content is not None:
        path.write_text(content)
    op = Opener(ok_bot())
    assert notify.bot_event(EVENT, secret_file=str(path), opener=op) is Outcome.NO_BOT
    assert op.requests == []


@pytest.mark.parametrize('body', [{'ok': True}, {'ok': False, 'event_id': 1}, {}, [1]])
def test_bot_event_подтверждение(secret_file, body):
    assert notify.bot_event(EVENT, secret_file=secret_file,
                            opener=Opener(Resp(200, body))) is Outcome.FAILED
    assert notify.bot_event(EVENT, secret_file=secret_file, confirm=False,
                            opener=Opener(Resp(200, body))) is Outcome.SENT


@pytest.mark.parametrize('reply', [
    http_error(403, {'ok': False}), ConnectionRefusedError(SECRET), socket.timeout(SECRET),
    Resp(200, b'not json ' + SECRET.encode()), Resp(500, {'ok': True, 'event_id': 1}),
    TimeoutError(), ValueError(SECRET)])
def test_bot_event_отказы_без_исключения_и_секрета(secret_file, reply):
    for confirm in (True, False):
        out = notify.bot_event(EVENT, secret_file=secret_file, confirm=confirm,
                               opener=Opener(reply))
        assert out is Outcome.FAILED
        assert SECRET not in repr(out) and SECRET not in str(out)


def test_flush_порядок_и_повтор(secret_file):
    pending = [{'n': 1}, {'n': 2}, {'n': 3}]
    op = Opener(ok_bot(), Resp(403, {}), ok_bot())
    assert notify.flush(pending, secret_file=secret_file, opener=op) is Outcome.FAILED
    assert pending == [{'n': 2}, {'n': 3}]
    op2 = Opener(ok_bot(2), ok_bot(3))
    assert notify.flush(pending, secret_file=secret_file, opener=op2) is Outcome.SENT
    assert pending == []
    assert [json.loads(r.data)['n'] for r, _ in op2.requests] == [2, 3]
    assert notify.flush(pending, secret_file=secret_file, opener=op2) is Outcome.SENT
    assert len(op2.requests) == 2


def test_flush_нет_секрета_очередь_цела(tmp_path):
    pending = [{'n': 1}]
    out = notify.flush(pending, secret_file=str(tmp_path / 'нет'), opener=Opener(ok_bot()))
    assert out is Outcome.NO_BOT and pending == [{'n': 1}]


def test_panel_event(secret_file):
    event = {'type': 'backup_failed', 'detail': 'нет пароля шифрования'}
    op = Opener(Resp(200, b''))
    assert notify.panel_event(event, secret_file=secret_file, opener=op) is Outcome.SENT
    req, _ = op.requests[0]
    assert req.get_header('Authorization') == f'Bearer {SECRET}'
    assert req.full_url == notify.PANEL_URL and json.loads(req.data) == event
    assert SECRET.encode() not in req.data


def test_panel_event_отказы(secret_file, tmp_path):
    assert notify.panel_event({}, secret_file=str(tmp_path / 'x'),
                              opener=Opener(Resp())) is Outcome.NO_BOT
    for reply in (http_error(401, {}), OSError(SECRET), Resp(503, b'')):
        out = notify.panel_event({}, secret_file=secret_file, opener=Opener(reply))
        assert out is Outcome.FAILED and SECRET not in repr(out)


def test_clean_detail():
    assert notify.clean_detail('') == ''
    assert notify.clean_detail('a\x00b\n\tc   d') == 'a b c d'
    assert notify.clean_detail(b'ok \xff\xfe end') == 'ok �� end'
    assert notify.clean_detail('нет пароля шифрования') == 'нет пароля шифрования'
    assert notify.clean_detail('x' * 500) == 'x' * 200
    assert notify.clean_detail('ж' * 500) == 'ж' * 200
    out = notify.clean_detail('😀' * 500)
    assert len(out) == 100 and notify.utf16_len(out) == 200
    assert notify.clean_detail('😀' * 3, limit=5) == '😀😀'
    assert notify.clean_detail(notify.clean_detail('\x00 привет \x07')) == 'привет'


def test_utf16_len():
    assert notify.utf16_len('') == 0
    assert notify.utf16_len('abc') == 3
    assert notify.utf16_len('привет') == 6
    assert notify.utf16_len('😀') == 2


def api(opener, base='https://api.telegram.org'):
    return BotApi(TOKEN, api_base=base, opener=opener)


def part(text='привет'):
    return {'method': 'sendMessage', 'text': text}


def test_call_успех_и_тело():
    op = Opener(Resp(200, {'ok': True, 'result': {'message_id': 9}}))
    reply = api(op).call(42, part())
    assert reply.ok and reply.result == {'message_id': 9} and not reply.final
    req, _ = op.requests[0]
    assert req.full_url == f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    assert json.loads(req.data) == {'chat_id': 42, 'text': 'привет',
                                    'link_preview_options': {'is_disabled': True}}


def test_call_edit_поля():
    op = Opener(Resp(200, {'ok': True}))
    p = {'method': 'editMessageText', 'text': 't', 'message_id': 5, 'reply_markup': {'a': 1}}
    assert api(op).call(1, p).ok
    sent = json.loads(op.requests[0][0].data)
    assert sent['message_id'] == 5 and sent['reply_markup'] == {'a': 1}


@pytest.mark.parametrize('params,expected', [
    ({'retry_after': 7}, 7), ({'retry_after': 'inf'}, 1), ({'retry_after': -3}, 1),
    ({}, 1), ({'retry_after': 0.2}, 1)])
def test_call_429(params, expected):
    body = {'ok': False, 'error_code': 429, 'parameters': params}
    reply = api(Opener(http_error(429, body))).call(1, part())
    assert (reply.ok, reply.final, reply.retry_after) == (False, False, expected)
    assert reply.reason == 'Telegram 429'


@pytest.mark.parametrize('code', [400, 403])
def test_call_постоянные(code):
    body = {'ok': False, 'error_code': code, 'description': 'Forbidden: bot was blocked by the user'}
    reply = api(Opener(http_error(code, body))).call(1, part())
    assert reply.final and not reply.ok
    assert reply.reason == f'Telegram {code}: бот заблокирован пользователем'


def test_call_неизвестное_описание_и_5xx():
    reply = api(Opener(http_error(500, {'ok': False, 'description': 'secret ' + TOKEN}))).call(1, part())
    assert reply.reason == 'Telegram 500' and not reply.final
    assert not api(Opener(Resp(200, {'ok': False, 'error_code': 'x'}))).call(1, part()).final


@pytest.mark.parametrize('reply', [
    OSError(TOKEN), ConnectionResetError(TOKEN), TimeoutError(TOKEN), ValueError(TOKEN),
    Resp(200, b'{"ok": tru'), Resp(200, b''), Resp(200, b'[1]'),
    Resp(200, b'{"ok": true' + b' ' * 70000 + b'}')])
def test_call_сеть_и_ответ_без_токена(reply):
    out = api(Opener(reply)).call(1, part())
    assert not out.ok and not out.final and out.retry_after == 0
    assert out.reason == 'Telegram недоступен или ответ не подтверждён'
    assert TOKEN not in repr(out) and TOKEN not in out.reason and TOKEN not in str(tuple(out))


def test_call_редирект_не_исполняется():
    class Req:
        pass
    handler = notify._NoRedirect()
    assert handler.redirect_request(Req(), None, 302, 'm', {}, 'http://evil/') is None
    # без подмены настоящий opener на 302 бросает HTTPError, а не идёт по адресу
    reply = api(Opener(http_error(302, {}))).call(1, part())
    assert not reply.ok and not reply.final and reply.reason == 'Telegram 302'


def test_call_документ_не_ascii():
    op = Opener(Resp(200, {'ok': True}))
    doc = {'method': 'sendDocument', 'caption': 'подпись 😀', 'filename': 'журнал-ß.txt',
           'document': 'строка1\nстрока2 😀'}
    assert api(op).call(77, doc).ok
    req, _ = op.requests[0]
    assert req.full_url.endswith('/sendDocument')
    assert req.get_header('Content-type').startswith('multipart/form-data; boundary=')
    text = req.data.decode('utf-8')
    for piece in ('подпись 😀', 'filename="журнал-ß.txt"', 'строка1\nстрока2 😀', '77'):
        assert piece in text


def test_api_base_проверка():
    for bad in ('http://example.org', 'ftp://127.0.0.1', 'api.telegram.org'):
        with pytest.raises(ValueError):
            BotApi(TOKEN, api_base=bad, opener=Opener())
    BotApi(TOKEN, api_base='http://127.0.0.1:8081', opener=Opener())
    BotApi(TOKEN, api_base='http://[::1]:8081', opener=Opener())


def test_reachable():
    op = Opener(Resp(200, {}))
    assert api(op).reachable()
    assert op.requests[0][0].full_url == f'https://api.telegram.org/bot{TOKEN}/getMe'
    assert api(Opener(http_error(401, {}))).reachable()
    assert not api(Opener(OSError(TOKEN))).reachable()
    assert not api(Opener(TimeoutError())).reachable()


def test_opener_по_умолчанию_один():
    first, second = BotApi(TOKEN), BotApi(TOKEN)
    assert first._opener is second._opener is notify._OPENER


def test_repr_без_токена():
    assert TOKEN not in repr(BotApi(TOKEN))
