#!/usr/bin/env python3
"""
failover-watchdog.py — быстрый failover экзит-нод для anysda-vpn.

sing-box urltest снимается с мёртвой ноды ~33с. Этот вотчдог активно
опрашивает экзиты через clash-api и сразу переключает selector-группу
foreign-best на живой экзит с наименьшей задержкой.

Что важно для скорости (см. test-notes #14 — старый вотчдог давал ~20с):
  - опрос всех экзитов параллельный, тик НЕ виснет дольше JOIN_DEADLINE.
    clash-api для МЁРТВОГО QUIC-экзита игнорит свой параметр timeout и
    висит до ~7с — раньше это растягивало каждый тик и failover до ~20с;
  - подтверждение «на месте»: если текущий экзит не ответил, вотчдог НЕ
    ждёт следующий тик, а тут же до-проверяет его ещё (DEAD_AFTER-1) раз
    подряд с коротким зазором. Анти-флап сохранён (одиночный транзиентный
    промах не переключает), но failover ~5-7с вместо ~20с.

Группа WATCH_GROUP (foreign-best) в конфиге роутера должна быть
type=selector (см. gen-router-config.py) — управляется через clash-api PUT.

Конфиг — через переменные окружения (EnvironmentFile systemd-юнита):
  CLASH_API         базовый URL clash-api          http://10.99.0.1:9090
  CLASH_SECRET      Bearer-секрет clash-api         (обязателен)
  WATCH_GROUP       тег selector-группы             foreign-best
  PROBE_URL         URL для delay-теста             http://www.gstatic.com/generate_204
  INTERVAL          период опроса, сек              2
  PROBE_TIMEOUT_MS  таймаут delay-теста, мс         1500
  TOLERANCE_MS      порог переключения по латентности, мс  120
  DEAD_AFTER        промахов подряд до признания экзита мёртвым  2
  CONFIRM_GAP       зазор между до-проверками, сек  0.4
  LATENCY_HOLD      тиков подряд с преимуществом до switch-по-латентности  4
  COOLDOWN_S        сек штрафной для умершего экзита  60

Анти-флап по латентности: замеры delay через QUIC шумят ±300-500мс. Чтобы
вотчдог не метался между экзитами, переключение по СКОРОСТИ (не по смерти)
происходит лишь когда один и тот же экзит лучше текущего на >TOLERANCE
LATENCY_HOLD тиков ПОДРЯД. Смерть экзита по-прежнему переключает сразу.

Штрафная скамья (см. test-notes #16): экзит, признанный мёртвым,
COOLDOWN_S секунд НЕ выбирается обратно как foreign-best. Без этого при
flap-шторме (экзит дёргается up/down) вотчдог оптимистично возвращался на
него, едва тот поднимался, и ловил компаундные простои. В штрафной экзит
игнорируется при выборе best; если ВСЕ живые в штрафной — берётся лучший
среди всех (не стрэндим).
"""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

CLASH       = os.environ.get('CLASH_API', 'http://10.99.0.1:9090').rstrip('/')
SECRET      = os.environ.get('CLASH_SECRET', '')
GROUP       = os.environ.get('WATCH_GROUP', 'foreign-best')
# не-Cloudflare URL: до cp.cloudflare.com WARP-выходы быстрее direct и замеры
# врут; gstatic нейтрален.
PROBE_URL   = os.environ.get('PROBE_URL', 'http://www.gstatic.com/generate_204')
INTERVAL    = float(os.environ.get('INTERVAL', '2'))
TIMEOUT_MS  = int(os.environ.get('PROBE_TIMEOUT_MS', '1500'))
TOLERANCE   = int(os.environ.get('TOLERANCE_MS', '120'))
# анти-флап: экзит мёртв только после DEAD_AFTER промахов ПОДРЯД.
DEAD_AFTER  = int(os.environ.get('DEAD_AFTER', '2'))
CONFIRM_GAP = float(os.environ.get('CONFIRM_GAP', '0.4'))
# анти-флап по латентности: switch-по-скорости лишь после LATENCY_HOLD
# тиков подряд с устойчивым преимуществом одного и того же экзита.
LATENCY_HOLD = int(os.environ.get('LATENCY_HOLD', '4'))
_lat_cand: str = ''   # экзит-кандидат на switch-по-латентности
_lat_streak = 0       # сколько тиков подряд он держит преимущество
# штрафная скамья: умерший экзит COOLDOWN секунд не выбирается обратно.
COOLDOWN     = float(os.environ.get('COOLDOWN_S', '60'))
_penalty: dict = {}   # member -> monotonic-дедлайн пребывания в штрафной

# Жёсткие границы времени. clash-api для мёртвого QUIC-экзита игнорит
# параметр timeout и виснет — поэтому delay-пробу ограничиваем сами.
PROBE_HTTP_TIMEOUT = TIMEOUT_MS / 1000.0 + 0.5   # таймаут urlopen для delay-пробы
JOIN_DEADLINE      = PROBE_HTTP_TIMEOUT + 0.5    # тик не ждёт проб дольше этого
API_TIMEOUT        = 5.0                         # таймаут обычных GET/PUT группы


def _req(method, path, body=None, timeout=API_TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CLASH + path, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + SECRET)
    if data:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def _probe(tag, out):
    """delay-тест одного outbound; out[tag] = delay(ms) либо None если мёртв."""
    q = urllib.parse.urlencode({'url': PROBE_URL, 'timeout': TIMEOUT_MS})
    try:
        d = _req('GET', f'/proxies/{urllib.parse.quote(tag)}/delay?{q}',
                 timeout=PROBE_HTTP_TIMEOUT)
        v = d.get('delay')
        out[tag] = v if isinstance(v, int) and v > 0 else None
    except Exception:
        out[tag] = None


def _probe_all(members):
    """Параллельный опрос всех членов. Тик не виснет дольше JOIN_DEADLINE —
    мёртвый экзит не тормозит остальных."""
    out: dict = {}
    threads = [threading.Thread(target=_probe, args=(m, out), daemon=True)
               for m in members]
    for t in threads:
        t.start()
    deadline = time.monotonic() + JOIN_DEADLINE
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return {m: out.get(m) for m in members}


def _switch(target, delay_ms, reason):
    _req('PUT', f'/proxies/{urllib.parse.quote(GROUP)}', {'name': target})
    print(f'switch -> {target} ({delay_ms}ms; {reason})', flush=True)


def _pick_best(alive):
    """Лучший по латентности экзит из НЕ-оштрафованных. Если все живые в
    штрафной — берём лучший среди всех живых (не стрэндим трафик)."""
    t = time.monotonic()
    free = {m: d for m, d in alive.items() if _penalty.get(m, 0.0) <= t}
    pool = free or alive
    return min(pool, key=pool.get)


def tick():
    group = _req('GET', f'/proxies/{urllib.parse.quote(GROUP)}')
    if group.get('type', '').lower() != 'selector':
        raise RuntimeError(f'{GROUP}: ожидался selector, получен {group.get("type")}')
    members = group.get('all', [])
    now = group.get('now')
    if not members:
        return

    delays = _probe_all(members)
    alive = {m: d for m, d in delays.items() if d is not None}
    if not alive:
        print('все экзиты не отвечают — выбор не трогаю', flush=True)
        return
    best = _pick_best(alive)   # лучший из не-оштрафованных

    global _lat_cand, _lat_streak
    if now in alive:
        # текущий жив — switch лишь по УСТОЙЧИВОМУ преимуществу по скорости:
        # один и тот же экзит лучше на >TOLERANCE LATENCY_HOLD тиков подряд
        # (анти-флап — замеры delay через QUIC шумят).
        if best != now and alive[best] + TOLERANCE < alive[now]:
            _lat_streak = _lat_streak + 1 if best == _lat_cand else 1
            _lat_cand = best
            if _lat_streak >= LATENCY_HOLD:
                _switch(best, alive[best],
                        f'{alive[now]}ms медленнее, устойчиво ×{_lat_streak}')
                _lat_cand, _lat_streak = '', 0
        else:
            _lat_cand, _lat_streak = '', 0
        return
    _lat_cand, _lat_streak = '', 0   # текущий не ответил — копилку латентности сбросить

    if now not in members:
        # выбора нет / он не из группы — просто берём лучший
        _switch(best, alive[best], 'выбор не задан')
        return

    # Текущий — член группы, но не ответил. НЕ ждём следующий тик: тут же
    # до-проверяем его ещё (DEAD_AFTER-1) раз подряд. Ожил на любой — транзиент.
    for k in range(1, DEAD_AFTER):
        time.sleep(CONFIRM_GAP)
        c: dict = {}
        _probe(now, c)
        if c.get(now) is not None:
            print(f'{now}: промах, но до-проверка {k}/{DEAD_AFTER - 1} ожила '
                  f'({c[now]}ms) — транзиент, selector не трогаю', flush=True)
            return

    # умерший экзит — в штрафную: COOLDOWN сек не вернёмся на него (анти-flap)
    _penalty[now] = time.monotonic() + COOLDOWN
    _switch(best, alive[best],
            f'{now} мёртв ({DEAD_AFTER}× промахов) — в штрафной {COOLDOWN:.0f}с')


def main():
    if not SECRET:
        print('CLASH_SECRET не задан', file=sys.stderr)
        sys.exit(1)
    print(f'failover-watchdog: group={GROUP} interval={INTERVAL}s '
          f'probe_timeout={TIMEOUT_MS}ms dead_after={DEAD_AFTER} '
          f'tolerance={TOLERANCE}ms latency_hold={LATENCY_HOLD} '
          f'cooldown={COOLDOWN:.0f}s api={CLASH}', flush=True)
    while True:
        try:
            tick()
        except Exception as e:
            print(f'tick error: {e}', file=sys.stderr, flush=True)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
