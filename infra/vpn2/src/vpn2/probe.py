"""Клиент clash API sing-box и проба задержки outbound'а (GET /proxies/<tag>/delay)."""

import functools
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from vpn2 import fsutil

LOCAL_CONTROLLERS = ('127.0.0.1:9090', 'localhost:9090', '[::1]:9090')


@functools.cache
def _default_opener():
    """Один opener на процесс, без прокси окружения: новый на каждый тик копил память."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe_error(exc):
    """Текст причины промаха для журнала: код ответа, таймаут, отказ соединения, прочее."""
    if isinstance(exc, urllib.error.HTTPError):
        return f'код {exc.code}'
    reason = getattr(exc, 'reason', exc)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return 'таймаут'
    if isinstance(reason, ConnectionRefusedError):
        return 'отказ соединения'
    return f'ошибка: {reason}'


def _q(part):
    """Сегмент пути: экранируется всё, включая '/'."""
    return urllib.parse.quote(part, safe='')


def _good_delay(value):
    """Годная задержка: int или float, не bool, больше нуля."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


class ClashApi:
    """JSON-клиент clash API: Bearer secret (пустой тоже шлётся), opener подменяем."""

    def __init__(self, base, secret, *, opener=None):
        if not isinstance(base, str) or not base.startswith(('http://', 'https://')) \
                or base.endswith('/'):
            raise ValueError(f'неверный base clash API: {base!r}')
        self.base = base
        self.secret = secret
        self.opener = opener if opener is not None else _default_opener()

    @classmethod
    def from_config(cls, path_or_dict, *, opener=None, local_only=False, required=True):
        """Клиент по experimental.clash_api из config.json sing-box."""
        config = path_or_dict if isinstance(path_or_dict, dict) \
            else fsutil.read_json(path_or_dict)
        try:
            api = config['experimental']['clash_api']
            controller = api['external_controller']
        except (KeyError, TypeError):
            if required:
                raise KeyError('experimental.clash_api.external_controller') from None
            return None
        if local_only and controller not in LOCAL_CONTROLLERS:
            raise ValueError('проба требует локальный clash API на порту 9090')
        return cls(f'http://{controller}', api.get('secret', ''), opener=opener)

    def request(self, method, path, body=None, *, timeout):
        """JSON-запрос; пустой ответ даёт {}; ошибки сети и битый JSON уходят наружу."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header('Authorization', 'Bearer ' + self.secret)
        if data:
            req.add_header('Content-Type', 'application/json')
        with self.opener.open(req, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    def proxies(self, *, timeout):
        """GET /proxies -> словарь outbound'ов."""
        return self.request('GET', '/proxies', timeout=timeout).get('proxies') or {}

    def proxy(self, name, *, timeout):
        """GET /proxies/<name>."""
        return self.request('GET', f'/proxies/{_q(name)}', timeout=timeout)

    def select(self, group, name, *, timeout):
        """PUT /proxies/<group> {'name': name}."""
        self.request('PUT', f'/proxies/{_q(group)}', {'name': name},
                     timeout=timeout)

    def connections(self, *, timeout):
        """GET /connections -> список соединений."""
        return self.request('GET', '/connections', timeout=timeout).get('connections') or []

    def close(self, conn_id, *, timeout):
        """DELETE /connections/<id>."""
        self.request('DELETE', f'/connections/{_q(str(conn_id))}',
                     timeout=timeout)

    def delay(self, tag, url, *, timeout_ms, http_timeout):
        """Одна проба: (ms, '') либо (None, причина); не бросает."""
        query = urllib.parse.urlencode({'url': url, 'timeout': timeout_ms})
        try:
            answer = self.request('GET', f'/proxies/{_q(tag)}/delay?{query}',
                                  timeout=http_timeout)
            value = answer.get('delay') if isinstance(answer, dict) else None
        except Exception as exc:
            return None, probe_error(exc)
        if _good_delay(value):
            return value, ''
        return None, f'clash-api без delay: {answer!r}'

    def delay_many(self, tags, url, *, timeout_ms, http_timeout, deadline):
        """Параллельные пробы, поток на тег, общий срок deadline с.

        Возвращает ({tag: ms|None}, {tag: причина}); не уложившийся тег - None без причины."""
        results = {}

        def work(tag):
            results[tag] = self.delay(tag, url, timeout_ms=timeout_ms,
                                      http_timeout=http_timeout)

        threads = [threading.Thread(target=work, args=(tag,), daemon=True) for tag in tags]
        for thread in threads:
            thread.start()
        end = time.monotonic() + deadline
        for thread in threads:
            thread.join(max(0.0, end - time.monotonic()))
        delays = {}
        errors = {}
        for tag in tags:
            ms, reason = results.get(tag, (None, ''))
            delays[tag] = ms
            if reason:
                errors[tag] = reason
        return delays, errors

    def alive_any(self, tag, urls, *, timeout_ms, http_timeout):
        """Цели по очереди до первой годной: две цели против ложного отказа сайта."""
        for url in urls:
            ms, _ = self.delay(tag, url, timeout_ms=timeout_ms, http_timeout=http_timeout)
            if ms is not None:
                return True
        return False
