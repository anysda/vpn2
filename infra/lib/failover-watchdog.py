#!/usr/bin/env python3
"""
failover-watchdog.py — быстрый failover экзит-нод для anysda-vpn.

sing-box urltest снимается с мёртвой ноды ~33с: он опрашивает раз в `interval`,
а зависшее QUIC-соединение к мёртвому экзиту висит ~30с до признания дохлым.
Этот вотчдог активно опрашивает экзиты через clash-api раз в INTERVAL секунд
с коротким таймаутом и сразу переключает selector-группу на живой экзит с
наименьшей задержкой — failover ~3-5с.

Группа WATCH_GROUP (foreign-best) в конфиге роутера должна быть type=selector
(см. gen-router-config.py) — selector управляется через clash-api `PUT`.

Конфиг — через переменные окружения (EnvironmentFile systemd-юнита):
  CLASH_API         базовый URL clash-api          http://10.99.0.1:9090
  CLASH_SECRET      Bearer-секрет clash-api         (обязателен)
  WATCH_GROUP       тег selector-группы             foreign-best
  PROBE_URL         URL для delay-теста             http://cp.cloudflare.com/generate_204
  INTERVAL          период опроса, сек              3
  PROBE_TIMEOUT_MS  таймаут delay-теста, мс         2000
  TOLERANCE_MS      порог переключения по латентности, мс  50
"""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

CLASH      = os.environ.get('CLASH_API', 'http://10.99.0.1:9090').rstrip('/')
SECRET     = os.environ.get('CLASH_SECRET', '')
GROUP      = os.environ.get('WATCH_GROUP', 'foreign-best')
PROBE_URL  = os.environ.get('PROBE_URL', 'http://cp.cloudflare.com/generate_204')
INTERVAL   = float(os.environ.get('INTERVAL', '3'))
TIMEOUT_MS = int(os.environ.get('PROBE_TIMEOUT_MS', '2000'))
TOLERANCE  = int(os.environ.get('TOLERANCE_MS', '50'))


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CLASH + path, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + SECRET)
    if data:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=TIMEOUT_MS / 1000.0 + 5) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def _probe(tag, out):
    """delay-тест одного outbound; out[tag] = delay(ms) либо None если мёртв."""
    q = urllib.parse.urlencode({'url': PROBE_URL, 'timeout': TIMEOUT_MS})
    try:
        d = _req('GET', f'/proxies/{urllib.parse.quote(tag)}/delay?{q}')
        v = d.get('delay')
        out[tag] = v if isinstance(v, int) and v > 0 else None
    except Exception:
        out[tag] = None


def tick():
    group = _req('GET', f'/proxies/{urllib.parse.quote(GROUP)}')
    if group.get('type', '').lower() != 'selector':
        raise RuntimeError(f'{GROUP}: ожидался selector, получен {group.get("type")}')
    members = group.get('all', [])
    now = group.get('now')
    if not members:
        return

    delays = {}
    threads = [threading.Thread(target=_probe, args=(m, delays)) for m in members]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    alive = {m: d for m, d in delays.items() if d is not None}
    if not alive:
        print('все экзиты не отвечают — выбор не трогаю', flush=True)
        return

    best = min(alive, key=alive.get)
    # текущий жив и не медленнее лучшего больше чем на TOLERANCE — оставляем
    if now in alive and alive[best] + TOLERANCE >= alive[now]:
        return

    reason = 'мёртв' if now not in alive else f'{alive[now]}ms'
    _req('PUT', f'/proxies/{urllib.parse.quote(GROUP)}', {'name': best})
    print(f'switch {now} ({reason}) -> {best} ({alive[best]}ms); alive={sorted(alive)}',
          flush=True)


def main():
    if not SECRET:
        print('CLASH_SECRET не задан', file=sys.stderr)
        sys.exit(1)
    print(f'failover-watchdog: group={GROUP} interval={INTERVAL}s '
          f'timeout={TIMEOUT_MS}ms tolerance={TOLERANCE}ms api={CLASH}', flush=True)
    while True:
        try:
            tick()
        except Exception as e:
            print(f'tick error: {e}', file=sys.stderr, flush=True)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
