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
  COOLDOWN_S        базовый штраф для умершего экзита, сек  60
  MAX_COOLDOWN_S    потолок штрафа при рецидивах, сек  600
  PENALTY_RESET_S   стабильности до сброса эскалации, сек  300
  DISABLED_EXITS_FILE  узлы, выключенные из панели  /etc/anysda/exits-disabled.json
  BALANCE           on|off — балансировка дорожек lane-NN по экзитам  off
  BALANCE_MARGIN_MS узел «быстрый», если задержка ≤ лучшей + N мс  150
  ACTIVE_BPS        порог активности дорожки, байт/с  20000
  REBALANCE_INTERVAL_S  период проверки дисбаланса, сек  300
  IMBALANCE_SHARE   разрыв max−min от общей нагрузки для переезда  0.35
  IMBALANCE_MIN_MBPS  и не меньше стольких Мбит/с  5
  MIN_DWELL_S       активную дорожку не двигать чаще, сек  1800
  IDLE_DWELL_S      неактивную — не чаще, сек  600
  RATE_TAU_S        постоянная сглаживания нагрузки, сек  300
  TG_GROUP          selector выхода бота (inbound tg-proxy)  tg-best
  TG_PROBE_URL      проба Bot API через экзит      https://api.telegram.org
  TG_INTERVAL_S     период проверки Bot API, сек    30

Выход бота (tg-best): Bot API (api.telegram.org) режется не везде одинаково —
gstatic-проба этого не видит. tg-best держим на экзите, с которого Bot API
отвечает: раз в TG_INTERVAL_S и сразу, если его узел выключен или выход мёртв.

Ручные маршруты панели (pin-<выход>): selector [выход, foreign-best]. Пока
узел выключен вручную или выход мёртв — смотрит на foreign-best, иначе
ручной маршрут без запасного выхода просто рвался бы.

Анти-флап по латентности: замеры delay через QUIC шумят ±300-500мс. Чтобы
вотчдог не метался между экзитами, переключение по СКОРОСТИ (не по смерти)
происходит лишь когда один и тот же экзит лучше текущего на >TOLERANCE
LATENCY_HOLD тиков ПОДРЯД. Смерть экзита по-прежнему переключает сразу.

Штрафная скамья (см. test-notes #16): экзит, признанный мёртвым,
COOLDOWN_S секунд НЕ выбирается обратно как foreign-best. Без этого при
flap-шторме (экзит дёргается up/down) вотчдог оптимистично возвращался на
него, едва тот поднимался, и ловил компаундные простои. В штрафной экзит
игнорируется при выборе best; если ВСЕ живые в штрафной — берётся лучший
среди всех (не стрэндим). При рецидиве (экзит умер снова вскоре после
окончания штрафа) срок удваивается до MAX_COOLDOWN_S — хронически
нестабильный экзит быстро паркуется надолго, система садится на стабильный.
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.error
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
# При рецидиве штраф удваивается (до MAX_COOLDOWN) — хронически нестабильный
# экзит паркуется надолго. PENALTY_RESET — после стольких секунд стабильной
# работы рецидив-счётчик сбрасывается на базовый COOLDOWN.
COOLDOWN      = float(os.environ.get('COOLDOWN_S', '60'))
MAX_COOLDOWN  = float(os.environ.get('MAX_COOLDOWN_S', '600'))
PENALTY_RESET = float(os.environ.get('PENALTY_RESET_S', '300'))
# Штраф — по УЗЛУ экзита, не по тегу: у ноды несколько outbound-ов
# (hy2-<node>-direct, hy2-<node>-warp) — они на одной ноде и умирают вместе,
# иначе watchdog обошёл бы штраф, перескочив на соседний тег того же узла.
_penalty: dict = {}   # node -> monotonic-дедлайн пребывания в штрафной
_pen_dur: dict = {}   # node -> текущая длительность штрафа (растёт при рецидиве)
# Ручное выключение узлов из панели: файл пишет панель (/api/ops/exits/:tag).
# Выключенный узел — как мёртвый: в выборе не участвует, если сейчас выбран —
# уводим сразу (без до-проверок), висящие через него соединения рвём.
# Выключение — предпочтение, а не запрет ценой простоя: если живых
# невыключенных нет, трафик идёт через живой выключенный и его соединения
# не рвём, пока не появится живой невыключенный.
# Нет файла / битый JSON / не список — ничего не выключено.
DISABLED_FILE = os.environ.get('DISABLED_EXITS_FILE', '/etc/anysda/exits-disabled.json')
_disabled: set = set()   # узлы, выключенные вручную (перечитывается каждый тик)
_disabled_warn = ''      # последняя жалоба на формат файла (пишем только смену)
_spared: set = set()     # выключенные узлы, через которые идём за неимением живых невыключенных

# Дорожки (lane-NN, см. gen-router-config.py): клиентские устройства разложены
# по selector'ам по VPN-IP; здесь решаем, на какой экзит смотрит каждая.
# BALANCE=off — дорожки просто повторяют foreign-best (поведение «один выход»).
# BALANCE=on  — активные устройства размазываются по «быстрым» узлам (задержка
# ≤ лучшей + BALANCE_MARGIN_MS); переезд — только при проблеме узла (сразу)
# или при сильном дисбалансе (раз в REBALANCE_INTERVAL_S, по одной дорожке,
# не чаще MIN_DWELL_S на дорожку). Переключение selector'а трогает только
# НОВЫЕ соединения — текущие доживают на старом выходе.
BALANCE            = os.environ.get('BALANCE', 'off').strip().lower() == 'on'
BALANCE_MARGIN_MS  = int(os.environ.get('BALANCE_MARGIN_MS', '150'))
ACTIVE_BPS         = float(os.environ.get('ACTIVE_BPS', '20000'))   # байт/с — дорожка «активна»
REBALANCE_INTERVAL = float(os.environ.get('REBALANCE_INTERVAL_S', '300'))
IMBALANCE_SHARE    = float(os.environ.get('IMBALANCE_SHARE', '0.35'))
IMBALANCE_MIN_BPS  = float(os.environ.get('IMBALANCE_MIN_MBPS', '5')) * 1e6 / 8
MIN_DWELL          = float(os.environ.get('MIN_DWELL_S', '1800'))   # активную дорожку не двигаем чаще
IDLE_DWELL         = float(os.environ.get('IDLE_DWELL_S', '600'))   # неактивную — чаще можно
RATE_TAU           = float(os.environ.get('RATE_TAU_S', '300'))     # сглаживание нагрузки (EWMA)
# «Медленный» решаем по СГЛАЖЕННОЙ задержке (QUIC-замеры шумят ±300-500мс,
# одиночный всплеск в 1-2с — норма) и только если она держится за порогом
# SLOW_HOLD_S подряд: вывод узла из балансировки двигает все его дорожки.
LAT_TAU            = float(os.environ.get('LAT_TAU_S', '30'))
SLOW_HOLD          = float(os.environ.get('SLOW_HOLD_S', '60'))
_lat_ewma: dict = {}      # узел -> сглаженная задержка, мс
_state_since: dict = {}   # узел -> monotonic, с которого статус «хочет» смениться
LANE_PREFIX = 'lane-'
_tick_alive = None        # {тег: задержка} живых невыключенных выходов последнего тика
_lane_rate: dict = {}     # lane -> EWMA байт/с
_lane_moved: dict = {}    # lane -> monotonic последнего переезда
_conn_seen: dict = {}     # conn id -> (up+down) на прошлом тике
_rate_t = None            # monotonic прошлого замера нагрузки
_elig: set = set()        # узлы, участвующие в балансировке (с гистерезисом)

_tag_miss: dict = {}      # выход -> промахов подряд (мёртв после DEAD_AFTER)
_tag_delay: dict = {}     # выход -> последняя удачная задержка
_elig_init = False        # первый удачный тик уже наполнил _elig
_last_rebalance = time.monotonic()

TG_GROUP     = os.environ.get('TG_GROUP', 'tg-best')
TG_PROBE_URL = os.environ.get('TG_PROBE_URL', 'https://api.telegram.org')
# Проба Bot API — тот же delay-тест clash-api: HEAD без перехода по редиректам,
# код ответа sing-box не смотрит и наружу не отдаёт. Смысл ответу даёт https:
# задержку вернёт только рукопожатие с настоящим сертификатом Telegram, заглушка
# блокировки так не ответит. Адрес на http:// clash-api молча меняет на свой
# gstatic, и Bot API тогда не проверялся бы вовсе.
if not TG_PROBE_URL.startswith('https://'):
    print(f'TG_PROBE_URL={TG_PROBE_URL}: нужен https://, беру https://api.telegram.org',
          file=sys.stderr, flush=True)
    TG_PROBE_URL = 'https://api.telegram.org'
TG_INTERVAL  = float(os.environ.get('TG_INTERVAL_S', '30'))
_tg_last = -1e9           # monotonic последней проверки Bot API
PIN_PREFIX = 'pin-'
_pin_miss: dict = {}      # выход ручного маршрута -> промахов подряд

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


def _classify_probe_error(e):
    """Текст причины промаха для журнала: таймаут / отказ соединения / код
    ответа, иначе следующий разбор снова упрётся в «следов нет»."""
    if isinstance(e, urllib.error.HTTPError):
        return f'код {e.code}'
    reason = getattr(e, 'reason', e)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return 'таймаут'
    if isinstance(reason, ConnectionRefusedError):
        return 'отказ соединения'
    return f'ошибка: {reason}'


def _probe(tag, out, errs=None, url=PROBE_URL):
    """delay-тест одного outbound; out[tag] = delay(ms) либо None если мёртв.
    errs[tag], если передан, получает причину промаха (см. _classify_probe_error)."""
    q = urllib.parse.urlencode({'url': url, 'timeout': TIMEOUT_MS})
    try:
        d = _req('GET', f'/proxies/{urllib.parse.quote(tag)}/delay?{q}',
                 timeout=PROBE_HTTP_TIMEOUT)
        v = d.get('delay')
        out[tag] = v if isinstance(v, int) and v > 0 else None
        if out[tag] is None and errs is not None:
            errs[tag] = f'clash-api без delay: {d!r}'
    except Exception as e:
        out[tag] = None
        if errs is not None:
            errs[tag] = _classify_probe_error(e)


def _probe_all(members, url=PROBE_URL):
    """Параллельный опрос всех членов. Тик не виснет дольше JOIN_DEADLINE —
    мёртвый экзит не тормозит остальных."""
    out: dict = {}
    errs: dict = {}
    threads = [threading.Thread(target=_probe, args=(m, out, errs, url), daemon=True)
               for m in members]
    for t in threads:
        t.start()
    deadline = time.monotonic() + JOIN_DEADLINE
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return {m: out.get(m) for m in members}, errs


def _switch(target, delay_ms, reason):
    _req('PUT', f'/proxies/{urllib.parse.quote(GROUP)}', {'name': target})
    print(f'switch -> {target} ({delay_ms}ms; {reason})', flush=True)


def _node_of(tag):
    """Узел экзита из тега hy2-<node>-<kind>. Для нестандартного — сам тег."""
    p = tag.split('-')
    return p[1] if len(p) >= 3 else tag


def _pick_best(alive):
    """Лучший по латентности экзит из НЕ-оштрафованных (штраф — по узлу).
    Если узлы всех живых в штрафной — берём лучший среди всех (не стрэндим)."""
    t = time.monotonic()
    free = {m: d for m, d in alive.items() if _penalty.get(_node_of(m), 0.0) <= t}
    pool = free or alive
    return min(pool, key=pool.get)


def _penalize(member):
    """Отправить УЗЕЛ экзита в штрафную (все его теги сразу). При рецидиве
    (умер снова вскоре после окончания прошлого штрафа) длительность
    удваивается до MAX_COOLDOWN — нестабильный узел паркуется надолго."""
    node = _node_of(member)
    t = time.monotonic()
    prev_dur = _pen_dur.get(node, 0.0)
    if prev_dur and (t - _penalty.get(node, 0.0)) < PENALTY_RESET:
        dur = min(prev_dur * 2, MAX_COOLDOWN)   # рецидив — эскалация
    else:
        dur = COOLDOWN                          # давно стабилен — базовый штраф
    _pen_dur[node] = dur
    _penalty[node] = t + dur
    return node, dur


def _read_disabled():
    """Узлы, выключенные вручную из панели. Логирует только изменения."""
    global _disabled, _disabled_warn
    warn = ''
    try:
        with open(DISABLED_FILE, encoding='utf-8') as f:
            data = json.load(f)
        raw = data.get('disabled', []) if isinstance(data, dict) else None
    except FileNotFoundError:
        raw = []
    except (OSError, ValueError) as e:
        raw, warn = [], f'не читается ({e})'
    if not isinstance(raw, list):
        warn = f'ожидался {{"disabled": [узлы]}}, получено {json.dumps(data, ensure_ascii=False)[:200]}'
        raw = []
    cur = {t for t in raw if isinstance(t, str)}
    if not warn and any(not isinstance(t, str) for t in raw):
        warn = f'не-строки в списке отброшены: {json.dumps(raw, ensure_ascii=False)[:200]}'
    if warn != _disabled_warn:
        if warn:
            print(f'{DISABLED_FILE}: {warn}; считаю выключенными: '
                  f'{", ".join(sorted(cur)) or "нет"}', flush=True)
        _disabled_warn = warn
    if cur != _disabled:
        print(f'выключены вручную: {", ".join(sorted(cur)) or "нет"}', flush=True)
        _disabled = cur
    return cur


def _excluded():
    """Узлы, которых сейчас избегаем: выключенные, кроме пощажённых."""
    return _disabled - _spared


def _spare(nodes):
    """Запомнить, через какие выключенные узлы идём (пусто — ни через какие).
    В журнал — только смена, одной строкой."""
    global _spared
    if nodes == _spared:
        return
    if nodes:
        print(f'живых невыключенных экзитов нет — трафик идёт через выключенные вручную '
              f'({", ".join(sorted(nodes))}), их соединения не рву', flush=True)
    else:
        print(f'есть живой невыключенный экзит — ухожу с выключенных вручную '
              f'({", ".join(sorted(_spared))})', flush=True)
    _spared = nodes


def _fetch_conns():
    """Снимок /connections на цикл (общий для учёта нагрузки дорожек и
    обрыва соединений через выключенные узлы). None — clash-api не ответил."""
    try:
        return _req('GET', '/connections').get('connections') or []
    except Exception as e:
        print(f'не смог получить соединения: {e}', file=sys.stderr, flush=True)
        return None


def _drop_disabled_conns(conns):
    """Рвём соединения, ещё идущие через выключенные узлы — как при сбое ноды
    (иначе долгие сессии текли бы через «выключенный» экзит до закрытия).
    mgmt-туннели не трогаем: по ним панель скрейпит метрики самого узла."""
    drop = _excluded()   # через пощажённые идёт трафик — их не трогаем
    if not drop or conns is None:
        return
    closed = 0
    for c in conns:
        out = (c.get('chains') or [''])[0]
        if out.endswith('-mgmt') or _node_of(out) not in drop or not c.get('id'):
            continue
        try:
            _req('DELETE', f'/connections/{urllib.parse.quote(c["id"])}')
            closed += 1
        except Exception:
            pass
    if closed:
        print(f'закрыто {closed} соединений через выключенные узлы '
              f'({", ".join(sorted(drop))})', flush=True)


def tick():
    global _tick_alive
    _tick_alive = None   # дорожки балансируем только по замерам удачного тика
    group = _req('GET', f'/proxies/{urllib.parse.quote(GROUP)}')
    if group.get('type', '').lower() != 'selector':
        raise RuntimeError(f'{GROUP}: ожидался selector, получен {group.get("type")}')
    disabled = _read_disabled()
    everyone = group.get('all', [])
    members = [m for m in everyone if _node_of(m) not in disabled]
    now = group.get('now')
    if not everyone:
        return

    delays, errs = _probe_all(members)
    alive = {m: d for m, d in delays.items() if d is not None}
    # Живых невыключенных нет — выбираем из живых выключенных: выключение не
    # стоит простоя. Пощажённые запоминаем, лишь решив идти через них (ниже):
    # транзиентный промах текущего не должен отменять обрыв соединений.
    fallback = not alive
    if fallback:
        members = everyone
        d2, e2 = _probe_all([m for m in everyone if _node_of(m) in disabled])
        errs.update(e2)
        alive = {m: d for m, d in d2.items() if d is not None}
    else:
        _spare(set())
    _tick_alive = alive
    if not alive:
        detail = ', '.join(f'{m}: {errs.get(m, "?")}' for m in members)
        print(f'все экзиты не отвечают, выбор не трогаю ({detail})', flush=True)
        return
    best = _pick_best(alive)   # лучший из не-оштрафованных
    spare = {_node_of(m) for m in alive} if fallback else set()

    global _lat_cand, _lat_streak
    if now in alive and fallback:
        _spare(spare)   # текущий выключен, но жив, а живых невыключенных нет — остаёмся
    if now and _node_of(now) in disabled and now not in alive:
        # текущий выключен вручную — уводим сразу, без до-проверок и штрафа
        _spare(spare)
        _lat_cand, _lat_streak = '', 0
        _switch(best, alive[best], f'узел {_node_of(now)} выключен вручную'
                                   f'{" и не отвечает" if fallback else ""}')
        return
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
        _spare(spare)
        _switch(best, alive[best], 'выбор не задан')
        return

    # Текущий — член группы, но не ответил. НЕ ждём следующий тик: тут же
    # до-проверяем его ещё (DEAD_AFTER-1) раз подряд. Ожил на любой — транзиент.
    last_err = errs.get(now, '?')
    for k in range(1, DEAD_AFTER):
        time.sleep(CONFIRM_GAP)
        c: dict = {}
        e: dict = {}
        _probe(now, c, e)
        if c.get(now) is not None:
            print(f'{now}: промах ({last_err}), но до-проверка {k}/{DEAD_AFTER - 1} '
                  f'ожила ({c[now]}ms), транзиент, selector не трогаю', flush=True)
            return
        last_err = e.get(now, last_err)

    # умерший узел — в штрафную целиком (анти-flap); при рецидиве срок растёт
    node, dur = _penalize(now)
    # best выбран до штрафа: без перевыбора уходили на соседний тег того же
    # узла (nl-direct -> nl-warp), и смерть узла давала второй простой.
    best = _pick_best(alive)
    _spare(spare)
    _switch(best, alive[best],
            f'{now} мёртв ({DEAD_AFTER}× промахов, последний: {last_err}), '
            f'узел {node} в штрафной {dur:.0f}с')


def _lane_of(conn):
    """Дорожка соединения — последний элемент chains (группа, через которую
    его провёл роутер): ['hy2-gb-direct', 'lane-07'] → 'lane-07'."""
    chains = conn.get('chains') or []
    return chains[-1] if chains and chains[-1].startswith(LANE_PREFIX) else None


def _update_lane_rates(conns):
    """EWMA нагрузки (байт/с, up+down) по дорожкам из дельт счётчиков
    соединений. Новое соединение берётся за базу (первые ≤2с не считаем) —
    иначе после рестарта вотчдога долгие соединения дали бы ложный всплеск."""
    global _rate_t
    t = time.monotonic()
    dt = (t - _rate_t) if _rate_t else 0.0
    _rate_t = t
    inst: dict = {}
    seen: dict = {}
    for c in conns:
        cid, lane = c.get('id'), _lane_of(c)
        if not cid or not lane:
            continue
        total = int(c.get('upload') or 0) + int(c.get('download') or 0)
        prev = _conn_seen.get(cid)
        seen[cid] = total
        if prev is not None and total >= prev:
            inst[lane] = inst.get(lane, 0) + (total - prev)
    _conn_seen.clear()
    _conn_seen.update(seen)
    if dt <= 0:
        return
    k = 1 - pow(2.718281828, -dt / RATE_TAU)
    for lane in set(_lane_rate) | set(inst):
        x = inst.get(lane, 0) / dt
        _lane_rate[lane] = _lane_rate.get(lane, 0.0) + (x - _lane_rate.get(lane, 0.0)) * k


def _effective_alive(alive, all_tags):
    """Живые выходы с анти-флапом: одиночный промах пробы не роняет выход —
    мёртвым он считается после DEAD_AFTER промахов подряд (до того берём
    последнюю удачную задержку). Выключенные вручную отбрасываем, кроме
    пощажённых (живых невыключенных нет — см. _spare)."""
    for tag in all_tags:
        if tag in alive:
            _tag_miss[tag] = 0
            _tag_delay[tag] = alive[tag]
        else:
            _tag_miss[tag] = _tag_miss.get(tag, DEAD_AFTER) + 1
    return {t: _tag_delay[t] for t in all_tags
            if _tag_miss.get(t, DEAD_AFTER) < DEAD_AFTER and t in _tag_delay
            and _node_of(t) not in _excluded()}


def _node_targets(alive):
    """Для каждого живого узла — выход, на который направляем дорожки
    (direct; warp — только если direct мёртв), и его задержка."""
    nodes: dict = {}
    for tag, d in alive.items():
        node = _node_of(tag)
        cur = nodes.get(node)
        is_direct = tag.endswith('-direct')
        if cur is None or (is_direct and not cur[0].endswith('-direct')):
            nodes[node] = (tag, d)
    return nodes


def _update_eligible(targets):
    """Множество «быстрых» узлов с гистерезисом. Выключенный вручную или без
    живых выходов (targets уже учитывают DEAD_AFTER) — вон сразу. По задержке
    решаем по EWMA (LAT_TAU): выход из балансировки — если сглаженная
    задержка хуже лучшей на > BALANCE_MARGIN_MS непрерывно SLOW_HOLD секунд;
    возврат — после LATENCY_HOLD тиков подряд в норме (он устройства не двигает)."""
    global _elig_init
    t = time.monotonic()
    k = 1 - pow(2.718281828, -INTERVAL / LAT_TAU)
    for n, (_, d) in targets.items():
        _lat_ewma[n] = d if n not in _lat_ewma else _lat_ewma[n] + (d - _lat_ewma[n]) * k
    for n in list(_lat_ewma):
        if n not in targets:
            del _lat_ewma[n]   # узел пропал — при возвращении начнём с чистого замера
    lat = {n: round(_lat_ewma[n]) for n in targets}
    best = min(lat.values(), default=None)
    want = {n for n in targets
            if best is not None and lat[n] <= best + BALANCE_MARGIN_MS
            and _penalty.get(n, 0.0) <= t}
    for node in set(want) | set(_elig):
        hard_out = node in _excluded() or node not in targets
        if hard_out:
            if node in _elig:
                _elig.discard(node)
                print(f'дорожки: узел {node} выбыл '
                      f'({"выключен вручную" if node in _excluded() else "нет живых выходов"})', flush=True)
            _state_since.pop(node, None)
            continue
        if (node in want) == (node in _elig):
            _state_since.pop(node, None)
            continue
        since = _state_since.setdefault(node, t)
        hold = LATENCY_HOLD * INTERVAL if node in want else SLOW_HOLD
        # первичное наполнение (первый удачный тик после старта) — без
        # ожидания, иначе первые секунды все дорожки свалились бы на один узел
        if t - since >= hold or not _elig_init:
            _state_since.pop(node, None)
            if node in want:
                _elig.add(node)
                print(f'дорожки: узел {node} в балансировке (~{lat[node]}ms)', flush=True)
            else:
                _elig.discard(node)
                print(f'дорожки: узел {node} вне балансировки — медленный '
                      f'(~{lat[node]}ms сглаж. ≥{SLOW_HOLD:.0f}с, лучший ~{best}ms)', flush=True)
    _elig_init = _elig_init or bool(targets)
    # не стрэндим: если «быстрых» не осталось — любой живой не выключенный
    return (_elig & set(targets)) or set(targets)


def _lane_ips(conns):
    ips: dict = {}
    for c in conns:
        lane = _lane_of(c)
        ip = (c.get('metadata') or {}).get('sourceIP')
        if lane and ip:
            ips.setdefault(lane, set()).add(ip)
    return ips


def _move_lane(lane, tag, reason, ips, quiet=False):
    """quiet — дорожка без соединений: в журнал не пишем поштучно (сводка у вызывающего)."""
    _req('PUT', f'/proxies/{urllib.parse.quote(lane)}', {'name': tag})
    _lane_moved[lane] = time.monotonic()
    if not quiet:
        who = ','.join(sorted(ips.get(lane, ()))[:3])
        print(f'{lane} ({who}) -> {tag}: {reason}', flush=True)


def lanes_tick(conns):
    """Раскладка дорожек по экзитам (см. описание BALANCE выше)."""
    global _last_rebalance
    proxies = _req('GET', '/proxies').get('proxies') or {}
    lanes = sorted(n for n, p in proxies.items()
                   if n.startswith(LANE_PREFIX) and str(p.get('type', '')).lower() == 'selector')
    if not lanes:
        return
    cur = {l: proxies[l].get('now') for l in lanes}

    if not BALANCE:
        fb = (proxies.get(GROUP) or {}).get('now')
        for l in lanes:
            if fb and cur[l] != fb:
                _req('PUT', f'/proxies/{urllib.parse.quote(l)}', {'name': fb})
        return

    if conns is not None:
        _update_lane_rates(conns)
    if _tick_alive is None:
        return   # замеров нет (ошибка тика) — ничего не двигаем
    all_tags = (proxies.get(lanes[0]) or {}).get('all') or []
    alive = _effective_alive(_tick_alive, all_tags)
    targets = _node_targets(alive)
    elig = _update_eligible(targets)
    if not elig:
        return   # ни одного живого экзита — «выбор не трогаю», как foreign-best
    ips = _lane_ips(conns or [])
    rate = {l: _lane_rate.get(l, 0.0) for l in lanes}
    load = {n: 0.0 for n in elig}
    count = {n: 0 for n in elig}   # дорожек на узле — тай-брейк при равной нагрузке
    broken = [l for l in lanes if _node_of(cur[l] or '') not in elig or cur[l] not in alive]
    for l in lanes:
        n = _node_of(cur[l] or '')
        if n in load and l not in broken:
            load[n] += rate[l]
            count[n] += 1
    lightest = lambda: min(load, key=lambda n: (load[n], count[n]))

    # 1. Проблема узла/выхода: дорожка не на «быстром» узле или её выход мёртв —
    #    уводим сразу, тяжёлые первыми, каждую на самый лёгкий узел.
    quiet_moves: dict = {}
    for l in sorted(broken, key=lambda x: -rate[x]):
        old = _node_of(cur[l] or '?')
        dst = lightest()
        why = ('узел выключен вручную' if old in _excluded()
               else 'выход мёртв' if cur[l] not in alive and old in elig
               else 'узел недоступен/медленный')
        quiet = l not in ips
        _move_lane(l, targets[dst][0], f'{old}→{dst}: {why}', ips, quiet)
        if quiet:
            quiet_moves.setdefault(f'{old}→{dst}: {why}', []).append(l[len(LANE_PREFIX):])
        load[dst] += rate[l]
        count[dst] += 1
        cur[l] = targets[dst][0]
    for what, ls in quiet_moves.items():
        print(f'{len(ls)} дорожек без соединений ({",".join(ls)}) — {what}', flush=True)

    t = time.monotonic()
    if t - _last_rebalance < REBALANCE_INTERVAL or len(load) < 2:
        return
    _last_rebalance = t

    # 2. Дисбаланс: одна активная дорожка с самого нагруженного узла на самый
    #    лёгкий — та, чья нагрузка ближе всего к половине разрыва.
    total = sum(load.values())
    hi, lo = max(load, key=load.get), min(load, key=load.get)
    gap = load[hi] - load[lo]
    if total > 0 and gap > IMBALANCE_SHARE * total and gap > IMBALANCE_MIN_BPS:
        cand = [l for l in lanes
                if _node_of(cur[l] or '') == hi and ACTIVE_BPS <= rate[l] < gap
                and t - _lane_moved.get(l, -1e9) >= MIN_DWELL]
        if cand:
            l = min(cand, key=lambda x: abs(rate[x] - gap / 2))
            _move_lane(l, targets[lo][0],
                       f'{hi}→{lo}: дисбаланс {load[hi] * 8 / 1e6:.1f}/{load[lo] * 8 / 1e6:.1f} Mbps', ips)
            load[hi] -= rate[l]
            load[lo] += rate[l]
            count[hi] -= 1
            count[lo] += 1
            cur[l] = targets[lo][0]

    # 3. Спящие дорожки — тихо разносим по узлам с упором на лёгкие:
    #    проснувшееся устройство попадёт туда, где свободнее. Двигаем ТОЛЬКО
    #    дорожки без единого соединения: устройство в сети, но с малым
    #    трафиком (мессенджер, фон) не должно менять внешний IP.
    idle = [l for l in lanes if rate[l] < ACTIVE_BPS]
    proj = dict(load)
    for l in idle:
        n = _node_of(cur[l] or '')
        if n in proj:
            proj[n] += ACTIVE_BPS   # вес «спящего» устройства
    levelled = []
    for l in idle:
        n = _node_of(cur[l] or '')
        if l in ips or t - _lane_moved.get(l, -1e9) < IDLE_DWELL:
            continue
        dst = min(proj, key=proj.get)
        if dst != n and n in proj and proj[n] - ACTIVE_BPS > proj[dst]:
            proj[n] -= ACTIVE_BPS
            proj[dst] += ACTIVE_BPS
            _move_lane(l, targets[dst][0], '', ips, quiet=True)
            levelled.append(f'{l[len(LANE_PREFIX):]}:{n}→{dst}')
    if levelled:
        print(f'выравнивание неактивных дорожек: {" ".join(levelled)}', flush=True)


def pins_tick(proxies):
    """pin-<выход> (ручные маршруты): на своём выходе, пока узел включён и выход
    жив (DEAD_AFTER промахов подряд — мёртв), иначе — на foreign-best."""
    if _tick_alive is None:
        return   # замеров нет (ошибка тика) — ничего не двигаем
    for name, p in proxies.items():
        if not name.startswith(PIN_PREFIX) or str(p.get('type', '')).lower() != 'selector':
            continue
        base = name[len(PIN_PREFIX):]
        _pin_miss[base] = 0 if base in _tick_alive else _pin_miss.get(base, 0) + 1
        disabled = _node_of(base) in _excluded()
        want = GROUP if disabled or _pin_miss[base] >= DEAD_AFTER else base
        if p.get('now') != want:
            _req('PUT', f'/proxies/{urllib.parse.quote(name)}', {'name': want})
            why = ('узел выключен вручную' if disabled else 'выход мёртв') if want == GROUP else 'выход снова доступен'
            print(f'{name} -> {want}: {why}', flush=True)


def tg_tick(proxies):
    """tg-best (выход бота): на экзите, с которого отвечает Bot API."""
    global _tg_last
    grp = proxies.get(TG_GROUP)
    if not grp or str(grp.get('type', '')).lower() != 'selector' or _tick_alive is None:
        return
    now = grp.get('now') or ''
    t = time.monotonic()
    now_bad = _node_of(now) in _excluded() or now not in _tick_alive
    if not now_bad and t - _tg_last < TG_INTERVAL:
        return
    _tg_last = t
    cands = [m for m in grp.get('all', []) if m in _tick_alive]   # живые и не выключенные
    if not cands:
        return
    res, errs = _probe_all(cands, TG_PROBE_URL)
    ok = {m: d for m, d in res.items() if d is not None}
    if now in ok:
        return
    if not ok:
        print(f'{TG_GROUP}: Bot API не отвечает ни через один экзит, выбор не трогаю', flush=True)
        return
    fb = (proxies.get(GROUP) or {}).get('now')
    target = fb if fb in ok else min(ok, key=ok.get)
    _req('PUT', f'/proxies/{urllib.parse.quote(TG_GROUP)}', {'name': target})
    why = ('узел выключен вручную' if _node_of(now) in _excluded()
           else 'выход мёртв' if now not in _tick_alive
           else f'Bot API не отвечает ({errs.get(now, "?")})')
    print(f'{TG_GROUP} -> {target} ({ok[target]}ms): {now or "—"}: {why}', flush=True)


def main():
    if not SECRET:
        print('CLASH_SECRET не задан', file=sys.stderr)
        sys.exit(1)
    print(f'failover-watchdog: group={GROUP} interval={INTERVAL}s '
          f'probe_timeout={TIMEOUT_MS}ms dead_after={DEAD_AFTER} '
          f'tolerance={TOLERANCE}ms latency_hold={LATENCY_HOLD} '
          f'cooldown={COOLDOWN:.0f}-{MAX_COOLDOWN:.0f}s api={CLASH} '
          f'balance={"on" if BALANCE else "off"} margin={BALANCE_MARGIN_MS}ms '
          f'rebalance={REBALANCE_INTERVAL:.0f}s imbalance>{IMBALANCE_SHARE:.0%}&>{IMBALANCE_MIN_BPS * 8 / 1e6:g}Mbps '
          f'dwell={MIN_DWELL:.0f}s tg={TG_GROUP}/{TG_INTERVAL:.0f}s', flush=True)
    while True:
        try:
            tick()
        except Exception as e:
            print(f'tick error: {e}', file=sys.stderr, flush=True)
        conns = _fetch_conns()
        try:
            lanes_tick(conns)
        except Exception as e:
            print(f'lanes error: {e}', file=sys.stderr, flush=True)
        try:
            proxies = _req('GET', '/proxies').get('proxies') or {}
            pins_tick(proxies)
            tg_tick(proxies)
        except Exception as e:
            print(f'pins/tg error: {e}', file=sys.stderr, flush=True)
        # после tick'ов: selector'ы уже уведены — рвём хвосты через выключенные узлы
        _drop_disabled_conns(conns)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
