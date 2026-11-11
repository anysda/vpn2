"""Сторож экзитов (vpn2 watchdog failover): выбор живого экзита на entry.

У каждого выхода и каждой ноги канала своя проба через clash-api раз в INTERVAL;
выход мёртв после DEAD_AFTER промахов подряд. Цикл проб не ждёт и раз в INTERVAL
по их итогам переключает selector-группы: ноги каналов hy2/TCP, foreign-best,
дорожки lane-NN, ручные маршруты pin-*, выход бота tg-best; когда мертвы все
каналы или завис выход при живом узле - перезапускает sing-box.

Настройки - окружение из /etc/anysda/failover-watchdog.env (vpn2.watchdog.failover_env).
"""
import json
import math
import os
import subprocess
import sys
import threading
import time

from vpn2 import fsutil, notify, probe


def _env(name, default, kind=float):
    try:
        return kind(os.environ[name])
    except (KeyError, ValueError):
        return kind(default)


CLASH = os.environ.get('CLASH_API', 'http://10.99.0.1:9090').rstrip('/')
SECRET = os.environ.get('CLASH_SECRET', '')
GROUP = os.environ.get('WATCH_GROUP', 'foreign-best')
INTERVAL = _env('INTERVAL', 2)
TIMEOUT_MS = _env('PROBE_TIMEOUT_MS', 3000, int)
DEAD_AFTER = _env('DEAD_AFTER', 3, int)
TOLERANCE = _env('TOLERANCE_MS', 120, int)       # смена по скорости: лучше на столько мс
LATENCY_HOLD = _env('LATENCY_HOLD', 4, int)      # ...столько циклов подряд
BALANCE = os.environ.get('BALANCE', 'on').strip().lower() == 'on'
WARP_RETURN = _env('WARP_RETURN_S', 600)         # дорожка с warp на direct своего узла
TCP_RETURN = _env('TCP_RETURN_S', 60)            # канал с TCP обратно на hy2
HY2_RISE = _env('HY2_RISE_S', 10)                # подъём QUIC после аварии канала
TG_INTERVAL = _env('TG_INTERVAL_S', 30)
RESTART_AFTER = _env('RESTART_AFTER_CHECKS', 3, int)
RESTART_MIN_INTERVAL = _env('RESTART_MIN_INTERVAL_S', 600)
STUCK_AFTER = _env('STUCK_AFTER_CHECKS', 30, int)
PRESSURE_MEM = _env('PRESSURE_MEM_AVG10', 10)
PRESSURE_CPU = _env('PRESSURE_CPU_AVG10', 60)

# не-Cloudflare адрес: до cp.cloudflare.com WARP-выходы быстрее direct, замеры врут.
PROBE_URL = 'http://www.gstatic.com/generate_204'
# Только https: задержку вернёт лишь рукопожатие с настоящим сертификатом Telegram.
TG_PROBE_URL = 'https://api.telegram.org'
TG_GROUP = 'tg-best'
# clash-api для мёртвого QUIC-выхода свой timeout не держит - ограничиваем сами.
HTTP_TIMEOUT = TIMEOUT_MS / 1000.0 + 0.5
API_TIMEOUT = 5.0
# Штрафная: умерший узел не выбирается COOLDOWN с; рецидив раньше PENALTY_RESET
# удваивает срок до MAX_COOLDOWN.
COOLDOWN, MAX_COOLDOWN, PENALTY_RESET = 60.0, 600.0, 300.0
# Выход мёртв, а невыбранная нога его канала жива: канал переведёт проход
# каналов, выбор ждёт его не дольше этого.
LEG_WAIT = HY2_RISE + INTERVAL + DEAD_AFTER * (HTTP_TIMEOUT + 0.5)
# Дорожки: «быстрый» узел - сглаженная задержка не хуже лучшей + MARGIN; медленным
# узел становится, продержавшись за порогом SLOW_HOLD.
MARGIN_MS, LAT_TAU, SLOW_HOLD = 150, 30.0, 60.0
ACTIVE_BPS, RATE_TAU = 20000.0, 300.0            # дорожка активна; сглаживание нагрузки
REBALANCE_INTERVAL, IMBALANCE_SHARE, IMBALANCE_MIN_BPS = 300.0, 0.35, 5e6 / 8
MIN_DWELL, IDLE_DWELL = 1800.0, 600.0            # стоянка активной и спящей дорожки
STUCK_MAX_HEALS = 2
LANE_STATE_FILE = '/var/lib/anysda-failover-watchdog/lanes.json'
RESTART_STATE_FILE = '/var/lib/anysda-failover-watchdog/restart.json'
DISABLED_FILE = '/etc/anysda/exits-disabled.json'
BOT_SECRET_FILE = '/etc/anysda/tgbot-secret.txt'
PSI_DIR, PROC_DIR = '/proc/pressure', '/proc'

clock = time.monotonic


def log(text):
    print(text, flush=True)


def node_of(tag):
    """Узел из тега hy2-<узел>-<вид>."""
    parts = tag.split('-')
    return parts[1] if len(parts) >= 3 else tag


def _selector(proxy):
    return str(proxy.get('type', '')).lower() == 'selector'


class Health:
    """Итоги проб: поток на тег, промахи подряд, последняя задержка, с какого времени жив."""

    def __init__(self, api):
        self.api, self.want, self.threads = api, set(), {}
        self.reset()

    def reset(self):
        self.miss, self.delay, self.since, self.died, self.why = {}, {}, {}, {}, {}
        self.started = clock()

    def record(self, tag, delay, why=''):
        if delay is not None:
            self.miss[tag], self.delay[tag] = 0, delay
            self.since.setdefault(tag, clock())
            self.died.pop(tag, None)
            return
        self.miss[tag], self.why[tag] = self.miss.get(tag, 0) + 1, why
        if self.miss[tag] == DEAD_AFTER:
            self.since.pop(tag, None)
            self.died[tag] = clock()

    def alive(self, tag):
        return tag in self.delay and self.miss[tag] < DEAD_AFTER

    def dead(self, tag):
        return self.miss.get(tag, 0) >= DEAD_AFTER

    def up(self, tag):
        """Сколько секунд тег жив без перерыва."""
        return clock() - self.since[tag] if tag in self.since else 0.0

    def forgive(self, tag):
        """Канал выхода переключён: прежние промахи шли по старой ноге."""
        self.miss[tag] = 0
        self.died.pop(tag, None)

    def settled(self, tags):
        """После запуска и перезапуска sing-box решений нет, пока о каждом теге нет вердикта."""
        return all(self.alive(t) or self.dead(t) for t in tags) \
            or clock() - self.started > DEAD_AFTER * (HTTP_TIMEOUT + INTERVAL)

    def watch(self, tags):
        self.want = set(tags)
        for tag in self.want - self.threads.keys():
            self.threads[tag] = threading.Thread(target=self._loop, args=(tag,), daemon=True)
            self.threads[tag].start()

    def _loop(self, tag):
        while tag in self.want:
            start = clock()
            delay, why = self.api.delay(tag, PROBE_URL, timeout_ms=TIMEOUT_MS, http_timeout=HTTP_TIMEOUT)
            # Отказ соединения - не ответил сам clash-api, о выходе это ничего не говорит.
            if why != 'отказ соединения':
                self.record(tag, delay, why)
            time.sleep(max(0.0, INTERVAL - (clock() - start)))
        del self.threads[tag]


def _plural(n, forms):
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    return forms[1] if 2 <= n % 10 <= 4 and not 10 <= n % 100 < 20 else forms[2]


def _psi(name):
    """some avg10 из /proc/pressure/<name>; None - PSI нет."""
    try:
        with open(os.path.join(PSI_DIR, name), encoding='ascii') as f:
            fields = dict(p.split('=') for line in f if line.startswith('some ') for p in line.split()[1:])
        return float(fields['avg10'])
    except (OSError, ValueError, KeyError):
        return None


def _top_rss(count=3):
    procs = []
    for pid in filter(str.isdigit, os.listdir(PROC_DIR)):
        try:
            with open(os.path.join(PROC_DIR, pid, 'status'), encoding='utf-8', errors='replace') as f:
                fields = dict(line.split(':', 1) for line in f if ':' in line)
            procs.append((int(fields['VmRSS'].split()[0]), fields['Name'].strip(), pid))
        except (OSError, ValueError, KeyError, IndexError):
            continue
    return ', '.join(f'{name} {pid} {rss // 1024}МБ' for rss, name, pid in sorted(procs, reverse=True)[:count])


def entry_pressure():
    """«память; PSI some avg10: память 35.0%, CPU 1.0%», когда entry под давлением, иначе ''."""
    mem, cpu = _psi('memory'), _psi('cpu')
    hot = [name for name, value, limit in (('память', mem, PRESSURE_MEM), ('CPU', cpu, PRESSURE_CPU))
           if value is not None and value >= limit]
    nums = ', '.join(f'{k} {v:.1f}%' for k, v in (('память', mem), ('CPU', cpu)) if v is not None)
    return f"{'/'.join(hot)}; PSI some avg10: {nums}" if hot else ''


def bot_event(subject, detail):
    event = {'type': 'system_alert', 'subject': subject, 'detail': detail}
    if notify.bot_event(event, secret_file=BOT_SECRET_FILE) is not notify.Outcome.SENT:
        print(f'бот: событие «{subject}» не доставлено', file=sys.stderr, flush=True)


def restart_sing_box():
    """'' - перезапущен, иначе причина отказа."""
    try:
        run = subprocess.run(['systemctl', 'restart', 'sing-box'],
                             capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return '' if run.returncode == 0 else run.stderr.strip() or f'код {run.returncode}'


def _stored(path):
    try:
        data = fsutil.read_json(path, {})
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _store(path, data):
    try:
        fsutil.atomic(path, json.dumps(data), mode=0o644)
    except OSError as e:
        print(f'{path}: не записать ({e})', file=sys.stderr, flush=True)


class Watchdog:
    def __init__(self, api=None):
        self.api = api or probe.ClashApi(CLASH, SECRET)
        self.health = Health(self.api)
        self.proxies, self.pairs, self.alive, self.up = {}, {}, {}, {}
        self.disabled, self.excluded, self.said = set(), set(), {}
        self.penalty, self.penalty_len = {}, {}      # узел -> до какого времени и на сколько
        self.candidate, self.streak = '', 0          # кто и сколько циклов подряд быстрее текущего
        self.tcp_nodes = set()                       # узлы, о переходе которых на TCP сказано
        self.all_dead, self.dead_streak = False, 0
        self.pin_dead, self.stuck_streak, self.heals = set(), {}, {}
        self.tg_miss, self.tg_next, self.tg_search = 0, 0.0, 0.0
        # Время на диске настенное: стоянка дорожек и предел частоты перезапуска
        # переживают перезапуск сторожа.
        shift = clock() - time.time()
        self.moved = {lane: min(clock(), at + shift) for lane, at in _stored(LANE_STATE_FILE).items()
                      if isinstance(at, (int, float))}
        self.saved = dict(self.moved)
        at = _stored(RESTART_STATE_FILE).get('restarted')
        self.restarted = min(clock(), at + shift) if isinstance(at, (int, float)) else None
        self.started = self.rebalanced = clock()
        self.rate, self.seen, self.rate_at = {}, {}, None
        self.fast, self.fast_ready, self.latency, self.turning, self.back = set(), False, {}, {}, {}

    def say(self, key, text):
        """Строка в журнал только при смене текста."""
        if self.said.get(key) != text:
            self.said[key] = text
            if text:
                log(text)

    def select(self, group, tag, why=''):
        self.api.select(group, tag, timeout=API_TIMEOUT)
        self.proxies[group]['now'] = tag
        log(f'{group} -> {tag}' + (f': {why}' if why else ''))

    def close(self, conns, skip):
        closed = 0
        for conn in conns or []:
            if conn.get('id') and not skip(conn.get('chains') or ['']):
                try:
                    self.api.close(conn['id'], timeout=API_TIMEOUT)
                    closed += 1
                except Exception:
                    pass
        return closed

    # --- цикл ---

    def cycle(self):
        self.proxies = proxies = self.api.proxies(timeout=API_TIMEOUT)
        try:
            conns = self.api.connections(timeout=API_TIMEOUT)
        except Exception as e:
            print(f'не смог получить соединения: {e}', file=sys.stderr, flush=True)
            conns = None
        self.members = (proxies.get(GROUP) or {}).get('all') or []
        self.pairs = {}
        for name, p in proxies.items():
            legs = p.get('all') or []
            if _selector(p) and len(legs) == 2 and legs[0].startswith('transport-hy2-') \
                    and legs[1] == 'transport-tcp-' + legs[0][14:]:
                self.pairs[name] = (legs[0], legs[1], *legs[0][14:].rpartition('-')[::2])
        groups = [p for n, p in proxies.items()
                  if _selector(p) and (n in (GROUP, TG_GROUP) or n.startswith(('lane-', 'pin-')))]
        exits = {t for p in groups for t in p.get('all') or [] if t.startswith('hy2-')}
        legs = {n for n in proxies if n.startswith(('transport-hy2-', 'transport-tcp-'))
                and not n.endswith('-mgmt')}
        self.health.watch(exits | legs)
        if not self.members or not self.health.settled(exits | legs):
            return
        self.transports()
        self.read_disabled()
        self.up = {t: self.health.delay[t] for t in exits if self.exit_alive(t)}
        enabled = {t: d for t, d in self.up.items() if node_of(t) not in self.disabled}
        # Выключение - предпочтение, а не запрет ценой простоя: живых невыключенных
        # нет - идём через живые выключенные и их соединения не рвём.
        spared = set() if enabled else {node_of(t) for t in self.up}
        self.say('spared', f'живых невыключенных экзитов нет - трафик идёт через выключенные вручную '
                           f'({", ".join(sorted(spared))}), их соединения не рву' if spared else '')
        self.excluded = self.disabled - spared
        self.alive = enabled or self.up
        self.exits()
        self.restart()
        self.lanes(conns)
        self.pins(conns)
        self.stuck()
        # mgmt не трогаем: по нему панель снимает метрики самого узла.
        closed = self.close(conns, lambda chains: chains[0].endswith('-mgmt')
                            or node_of(chains[0]) not in self.excluded)
        if closed:
            log(f'закрыто {closed} соединений через выключенные узлы ({", ".join(sorted(self.excluded))})')

    def read_disabled(self):
        """Узлы, выключенные из панели; нет файла или битый - ничего не выключено."""
        nodes, problem = [], ''
        try:
            nodes = fsutil.read_json(DISABLED_FILE, {}).get('disabled', [])
        except (OSError, ValueError, AttributeError) as e:
            problem = f'{DISABLED_FILE}: не читается ({e}), ничего не выключено'
        self.disabled = {n for n in nodes if isinstance(n, str)} if isinstance(nodes, list) else set()
        unknown = self.disabled - {node_of(m) for m in self.members}
        self.say('disabled-file', problem or (f'{DISABLED_FILE}: таких узлов нет, ничего не выключают: '
                                              f'{", ".join(sorted(unknown))}' if unknown else ''))
        self.say('disabled', f'выключены вручную: {", ".join(sorted(self.disabled)) or "нет"}')

    def leg_of(self, tag, spare=False):
        """Выбранная (spare - невыбранная) нога канала выхода; без запасного TCP - сам канал."""
        for selector, (hy2, tcp, *_) in self.pairs.items():
            if hy2 == 'transport-' + tag:
                on_tcp = self.proxies[selector].get('now') == tcp
                return (hy2 if on_tcp else tcp) if spare else (tcp if on_tcp else hy2)
        return None if spare or 'transport-' + tag not in self.proxies else 'transport-' + tag

    def exit_alive(self, tag):
        """Жив, либо мёртв вместе с выбранной ногой канала при живой запасной - её ещё ждём."""
        h, leg = self.health, self.leg_of(tag, spare=True)
        return h.alive(tag) or (tag in h.delay and leg is not None and h.alive(leg)
                                and not h.alive(self.leg_of(tag))
                                and clock() - h.died.get(tag, 0) < LEG_WAIT)

    def node_alive(self, tag):
        """Другой живой выход того же узла: беда канала или выхода, не узла."""
        return next((t for t in sorted(self.up) if t != tag and node_of(t) == node_of(tag)), '')

    def penalize(self, tag):
        node, t = node_of(tag), clock()
        again = node in self.penalty and t - self.penalty[node] < PENALTY_RESET
        self.penalty_len[node] = min(self.penalty_len[node] * 2, MAX_COOLDOWN) if again else COOLDOWN
        self.penalty[node] = t + self.penalty_len[node]
        return self.penalty_len[node]

    def free(self, tag):
        return self.penalty.get(node_of(tag), 0.0) <= clock()

    # --- каналы hy2/TCP ---

    def transports(self):
        """hy2 мёртв, TCP жив - канал на TCP; hy2 жив TCP_RETURN подряд или TCP мёртв - обратно.

        TCP, живой меньше HY2_RISE (после аварии канала или перезапуска sing-box),
        ещё не повод уходить с hy2: QUIC поднимается дольше. mgmt идёт за direct узла.
        """
        h, want = self.health, {}
        for selector, (hy2, tcp, node, kind) in self.pairs.items():
            if kind == 'mgmt':
                continue
            if self.proxies[selector].get('now') == tcp:
                back = h.alive(hy2) and (h.up(hy2) >= TCP_RETURN or h.dead(tcp))
                want[selector] = hy2 if back else tcp
            else:
                want[selector] = tcp if h.dead(hy2) and h.alive(tcp) and h.up(tcp) >= HY2_RISE else hy2
        on_tcp = {v[2]: want[s] == v[1] for s, v in self.pairs.items() if v[3] == 'direct'}
        for selector, (hy2, tcp, node, kind) in self.pairs.items():
            target = want.get(selector) or (tcp if on_tcp.get(node) else hy2)
            if self.proxies[selector].get('now') != target:
                self.select(selector, target)
                h.forgive(hy2[len('transport-'):])
        # Про UDP узла говорит direct: warp уходит на TCP и от шума выхода WARP.
        for node, tcp_now in sorted(on_tcp.items()):
            if tcp_now and node not in self.tcp_nodes:
                bot_event(f'UDP до экзита {node} недоступен, работаю через TCP',
                          'Hysteria2 до экзита не отвечает, каналы идут запасным VLESS + Reality '
                          'по TCP, sing-box не перезапускаю')
            elif not tcp_now and node in self.tcp_nodes:
                bot_event(f'UDP до экзита {node} снова работает',
                          f'Hysteria2 жив {TCP_RETURN:.0f} с подряд, каналы вернулись с TCP')
            (self.tcp_nodes.add if tcp_now else self.tcp_nodes.discard)(node)

    # --- foreign-best ---

    def exits(self):
        h, now = self.health, self.proxies[GROUP].get('now')
        pool = {m: self.alive[m] for m in self.members if m in self.alive}
        enabled = [m for m in self.members if node_of(m) not in self.disabled]
        spare = any(h.alive(tcp if self.proxies[s].get('now') == hy2 else hy2)
                    for s, (hy2, tcp, _, kind) in self.pairs.items() if kind != 'mgmt')
        self.all_dead = bool(enabled) and not self.up and all(h.dead(m) for m in enabled) and not spare
        self.say('none', '' if pool else 'все экзиты не отвечают, выбор не трогаю ('
                 + ', '.join(f'{m}: {h.why.get(m, "?")}' for m in self.members) + ')')
        if not pool:
            return
        free = {m: d for m, d in pool.items() if self.free(m)} or pool
        best = min(free, key=free.get)
        if now in pool:
            # Текущий жив: смена только по устойчивому преимуществу в скорости.
            if best != now and pool[best] + TOLERANCE < pool[now]:
                self.streak = self.streak + 1 if best == self.candidate else 1
                self.candidate = best
                if self.streak < LATENCY_HOLD:
                    return
                why = f'{pool[now]}ms медленнее, устойчиво ×{self.streak}'
            else:
                self.candidate, self.streak = '', 0
                return
        elif now not in self.members:
            why = 'выбор не задан'
        elif node_of(now) in self.disabled:
            why = f'узел {node_of(now)} выключен вручную'
        elif not h.dead(now):
            return
        else:
            why = f'{now} мёртв ({DEAD_AFTER}× промахов, последний: {h.why.get(now, "?")})'
            other = self.node_alive(now)
            if other:
                why += f', {other} узла {node_of(now)} жив, узел не штрафую'
            else:
                # Перевыбор после штрафа: иначе ушли бы на соседний выход того же узла.
                why += f', узел {node_of(now)} в штрафной {self.penalize(now):.0f}с'
                free = {m: d for m, d in pool.items() if self.free(m)} or pool
                best = min(free, key=free.get)
        self.candidate, self.streak = '', 0
        self.api.select(GROUP, best, timeout=API_TIMEOUT)
        self.proxies[GROUP]['now'] = best
        log(f'switch -> {best} ({pool[best]}ms; {why})')

    # --- перезапуск sing-box ---

    def restart_due(self):
        return self.restarted is None or clock() - self.restarted >= RESTART_MIN_INTERVAL

    def restart_now(self, what, subject='Экзиты'):
        self.restarted = clock()
        _store(RESTART_STATE_FILE, {'restarted': round(time.time(), 1)})
        log(f'{what} - перезапускаю sing-box')
        err = restart_sing_box()
        if err:
            print(f'sing-box не перезапустился: {err}', file=sys.stderr, flush=True)
        bot_event(subject, f'{what} - перезапуск sing-box не удался: {err}' if err
                  else f'{what} - перезапустил sing-box')
        self.health.reset()
        self.dead_streak = 0
        self.stuck_streak.clear()

    def restart(self):
        """Все каналы всех включённых узлов мертвы RESTART_AFTER проверок подряд."""
        self.dead_streak = self.dead_streak + 1 if self.all_dead else 0
        if RESTART_AFTER <= 0 or self.dead_streak < RESTART_AFTER or not self.restart_due():
            return
        checks = f"{self.dead_streak} {_plural(self.dead_streak, ('проверку', 'проверки', 'проверок'))} подряд"
        pressure = entry_pressure()
        if pressure:
            self.restart_now(f'entry перегружен ({pressure}; топ RSS: {_top_rss() or "-"}), '
                             f'sing-box не отвечает {checks}', 'Entry')
        else:
            self.restart_now(f'Все каналы мертвы {checks}')

    def stuck(self):
        """Выход ручного маршрута мёртв при живом канале до узла: завис в sing-box entry.

        STUCK_AFTER проверок подряд - перезапуск; не ожил после STUCK_MAX_HEALS
        перезапусков - одно событие, и больше не лечим, пока не оживёт.
        """
        h = self.health
        bases = sorted(n[4:] for n, p in self.proxies.items() if n.startswith('pin-') and _selector(p))
        stuck = []
        for base in bases:
            if base in self.up:
                self.heals.pop(base, None)
            elif not self.all_dead and h.dead(base) and node_of(base) not in self.excluded:
                leg = self.leg_of(base)
                # Без канала в конфиге узел жив по соседнему выходу, и только для direct.
                if h.alive(leg) if leg else base.endswith('-direct') and self.node_alive(base):
                    stuck.append(base)
        self.stuck_streak = {b: self.stuck_streak.get(b, 0) + 1 for b in stuck}
        due = []
        for base in stuck:
            heals = self.heals.get(base, 0)
            if RESTART_AFTER <= 0 or self.stuck_streak[base] < STUCK_AFTER or heals > STUCK_MAX_HEALS:
                continue
            if heals < STUCK_MAX_HEALS:
                due.append(base)
                continue
            self.heals[base] = heals + 1
            log(f'{base}: узел жив, а выход не ожил после {heals} перезапусков sing-box - больше не перезапускаю')
            bot_event('Экзиты', f'Выход {base} не оживает после перезапуска sing-box ({heals}×) - '
                                f'лечить перестал, маршрут идёт через {GROUP}')
        if due and self.restart_due():
            for base in stuck:
                self.heals[base] = min(self.heals.get(base, 0) + 1, STUCK_MAX_HEALS)
            self.restart_now(f'Выход {", ".join(due)} завис при живом узле')

    # --- ручные маршруты pin-* ---

    def pins(self, conns):
        """pin-<выход> на своём выходе, пока узел включён, выход жив и узел не в штрафной;
        иначе на foreign-best, а warp-выход - на живой WARP-ноге другого узла."""
        t = clock()
        for name, p in self.proxies.items():
            if not name.startswith('pin-') or not _selector(p):
                continue
            base = name[4:]
            node = node_of(base)
            if base in self.up:
                self.pin_dead.discard(base)
            elif self.health.dead(base) and base not in self.pin_dead:
                # Не основной выход exits() не штрафует: без штрафа маршрут вернулся бы
                # на первом удачном замере. Долгая смерть штраф не наращивает.
                self.pin_dead.add(base)
                if node not in self.excluded and self.free(base) and not self.node_alive(base):
                    log(f'{base} мёртв ({DEAD_AFTER}× промахов), узел {node} в штрафной {self.penalize(base):.0f}с')
            if node in self.excluded:
                why = 'узел выключен вручную'
            elif base not in self.up:
                if not self.health.dead(base):
                    continue
                why = 'выход мёртв'
            else:
                why = '' if self.free(base) else f'узел в штрафной ещё {self.penalty[node] - t:.0f}с'
            want = base
            if why and base.endswith('-warp'):
                # В foreign-best direct-ноги: «-warp» вышел бы с адреса дата-центра.
                legs = {x: d for x, d in self.alive.items() if x.endswith('-warp') and x != base and self.free(x)}
                want = p.get('now') if p.get('now') in legs else min(legs, key=legs.get) if legs else base
                self.say(name, f'{name}: {why}, живой WARP-ноги нет - остаюсь на {base}' if not legs else '')
            elif why:
                want = GROUP
            if p.get('now') != want:
                self.select(name, want, why or 'выход снова доступен')
                # Долгие соединения, открытые через запасной, иначе часами шли бы мимо выбранного экзита.
                if not why and self.close(conns, lambda chains: name not in chains or chains[0] == base):
                    log(f'{name}: закрыты соединения с запасного выхода')

    # --- дорожки ---

    def rates(self, conns):
        """Сглаженная нагрузка дорожек (байт/с) по приросту счётчиков соединений."""
        t, grown, seen = clock(), {}, {}
        for conn in conns:
            lane = (conn.get('chains') or [''])[-1]
            if conn.get('id') and lane.startswith('lane-'):
                seen[conn['id']] = total = int(conn.get('upload') or 0) + int(conn.get('download') or 0)
                grown[lane] = grown.get(lane, 0) + max(0, total - self.seen.get(conn['id'], total))
        dt, self.seen, self.rate_at = t - (self.rate_at or t), seen, t
        if dt > 0:
            k = 1 - math.exp(-dt / RATE_TAU)
            for lane in set(self.rate) | set(grown):
                self.rate[lane] = self.rate.get(lane, 0.0) + (grown.get(lane, 0) / dt - self.rate.get(lane, 0.0)) * k

    def eligible(self, targets):
        """«Быстрые» узлы с гистерезисом: выбывает медленный SLOW_HOLD подряд,
        возвращается после LATENCY_HOLD циклов в норме; мёртвый и выключенный - сразу."""
        t, k = clock(), 1 - math.exp(-INTERVAL / LAT_TAU)
        self.latency = {n: self.latency[n] + (d - self.latency[n]) * k if n in self.latency else d
                        for n, (_, d) in targets.items()}
        best = min(self.latency.values(), default=0)
        want = {n for n in targets if self.latency[n] <= best + MARGIN_MS and self.penalty.get(n, 0.0) <= t}
        for node in sorted(want | self.fast):
            if node not in targets:
                self.fast.discard(node)
                log(f'дорожки: узел {node} выбыл (нет живых выходов или выключен вручную)')
            elif (node in want) != (node in self.fast):
                hold = LATENCY_HOLD * INTERVAL if node in want else SLOW_HOLD
                if t - self.turning.setdefault(node, t) < hold and self.fast_ready:
                    continue
                (self.fast.add if node in want else self.fast.discard)(node)
                log(f'дорожки: узел {node} {"в балансировке" if node in want else "вне балансировки - медленный"} '
                    f'(~{self.latency[node]:.0f}ms, лучший ~{best:.0f}ms)')
            self.turning.pop(node, None)
        self.fast_ready = self.fast_ready or bool(targets)
        return (self.fast & set(targets)) or set(targets)

    def move(self, lane, tag, why=''):
        self.api.select(lane, tag, timeout=API_TIMEOUT)
        self.proxies[lane]['now'], self.moved[lane] = tag, clock()
        if why:
            log(f'{lane} -> {tag}: {why}')

    def lanes(self, conns):
        """Раскладка дорожек по экзитам; BALANCE=off - дорожки повторяют foreign-best."""
        lanes = sorted(n for n, p in self.proxies.items() if n.startswith('lane-') and _selector(p))
        cur = {lane: self.proxies[lane].get('now') or '' for lane in lanes}
        if not BALANCE:
            for lane in lanes:
                if cur[lane] != self.proxies[GROUP].get('now'):
                    self.api.select(lane, self.proxies[GROUP]['now'], timeout=API_TIMEOUT)
            return
        self.moved = {lane: self.moved.get(lane, self.started) for lane in lanes}
        if conns is not None:
            self.rates(conns)
        tags = self.proxies[lanes[0]].get('all') or [] if lanes else []
        alive = {x: self.alive[x] for x in tags if x in self.alive and node_of(x) not in self.excluded}
        targets = {}   # узел -> (выход, задержка): direct, warp - только когда direct мёртв
        for tag, delay in alive.items():
            if node_of(tag) not in targets or tag.endswith('-direct'):
                targets[node_of(tag)] = (tag, delay)
        fast = self.eligible(targets) if lanes else set()
        if not fast:
            return
        t, h = clock(), self.health
        busy = {(c.get('chains') or [''])[-1] for c in conns or []}
        rate = {lane: self.rate.get(lane, 0.0) for lane in lanes}
        load, count = dict.fromkeys(fast, 0.0), dict.fromkeys(fast, 0)
        broken = [lane for lane in lanes if node_of(cur[lane]) not in fast or cur[lane] not in alive]
        for lane in lanes:
            if lane not in broken:
                load[node_of(cur[lane])] += rate[lane]
                count[node_of(cur[lane])] += 1

        def put(lane, node, why=''):
            if node_of(cur[lane]) in load and lane not in broken:
                load[node_of(cur[lane])] -= rate[lane]
                count[node_of(cur[lane])] -= 1
            load[node] += rate[lane]
            count[node] += 1
            cur[lane] = targets[node][0]
            self.move(lane, cur[lane], why if lane in busy else '')

        # 1. Беда узла или выхода: уводим сразу, тяжёлые первыми, каждую на самый лёгкий узел.
        quiet = 0
        for lane in sorted(broken, key=lambda x: -rate[x]):
            old = node_of(cur[lane] or '?')
            why = ('узел выключен вручную' if old in self.excluded
                   else 'выход мёртв' if old in fast else 'узел недоступен/медленный')
            quiet += lane not in busy
            put(lane, min(load, key=lambda n: (load[n], count[n])), f'{old}: {why}')
        broken = []
        if quiet:
            log(f'{quiet} дорожек без соединений уведены с проблемных узлов')
        # 2. С warp на direct своего узла: direct жив без перерыва WARP_RETURN. Стоянку
        #    ждём, только если дорожка недавно уже возвращалась - direct мигает.
        for lane in lanes:
            direct = targets.get(node_of(cur[lane]), ('',))[0]
            calm = t - self.back.get(lane, -MIN_DWELL) >= MIN_DWELL or t - self.moved[lane] >= MIN_DWELL
            if cur[lane].endswith('-warp') and direct.endswith('-direct') and h.up(direct) >= WARP_RETURN and calm:
                self.move(lane, direct, f'возврат с {cur[lane]}, прямой жив без перерыва {h.up(direct) / 60:.1f} мин')
                cur[lane], self.back[lane] = direct, t
        # Без снимка соединений нагрузки неизвестны: дальше только плановое выравнивание.
        if conns is not None and t - self.rebalanced >= REBALANCE_INTERVAL and len(load) > 1:
            self.rebalanced = t
            # 3. Дисбаланс: одна активная дорожка с самого нагруженного узла на самый лёгкий.
            high, low = max(load, key=load.get), min(load, key=load.get)
            gap = load[high] - load[low]
            if gap > IMBALANCE_SHARE * sum(load.values()) and gap > IMBALANCE_MIN_BPS:
                heavy = [lane for lane in lanes if node_of(cur[lane]) == high
                         and ACTIVE_BPS <= rate[lane] < gap and t - self.moved[lane] >= MIN_DWELL]
                if heavy:
                    put(min(heavy, key=lambda x: abs(rate[x] - gap / 2)), low,
                        f'{high}→{low}: дисбаланс {load[high] * 8 / 1e6:.1f}/{load[low] * 8 / 1e6:.1f} Mbps')
            # 4. Дорожки без единого соединения тихо разносим поровну: проснувшееся
            #    устройство попадёт туда, где свободнее, а внешний IP никому не меняем.
            weight = {n: load[n] + ACTIVE_BPS * sum(1 for lane in lanes if node_of(cur[lane]) == n
                                                    and rate[lane] < ACTIVE_BPS) for n in load}
            for lane in lanes:
                node, light = node_of(cur[lane]), min(weight, key=weight.get)
                if rate[lane] < ACTIVE_BPS and lane not in busy and t - self.moved[lane] >= IDLE_DWELL \
                        and node in weight and weight[node] - ACTIVE_BPS > weight[light]:
                    weight[node] -= ACTIVE_BPS
                    weight[light] += ACTIVE_BPS
                    put(lane, light)
        if self.moved != self.saved:
            shift = time.time() - clock()
            _store(LANE_STATE_FILE, {lane: round(at + shift, 1) for lane, at in sorted(self.moved.items())})
            self.saved = dict(self.moved)

    # --- выход бота ---

    def tg(self):
        """tg-best на экзите, с которого отвечает Bot API.

        Текущий выход проверяем раз в TG_INTERVAL, после промаха - каждый проход;
        DEAD_AFTER промахов подряд, мёртвый или выключенный выход - ищем другой.
        Bot API молчит через все выходы - поиск не чаще раза в TG_INTERVAL.
        """
        group, alive, t = self.proxies.get(TG_GROUP), dict(self.alive), clock()
        if not group or not _selector(group) or not alive:
            return
        now = group.get('now') or ''
        if node_of(now) in self.excluded:
            why = 'узел выключен вручную'
        elif now not in alive:
            why = 'выход мёртв'
        elif not self.tg_miss and t < self.tg_next:
            return
        else:
            self.tg_next = t + TG_INTERVAL
            delay, err = self.api.delay(now, TG_PROBE_URL, timeout_ms=TIMEOUT_MS, http_timeout=HTTP_TIMEOUT)
            self.tg_miss = 0 if delay is not None else self.tg_miss + 1
            if self.tg_miss < DEAD_AFTER:
                return
            why = f'Bot API не отвечает ({err}), {self.tg_miss}× подряд'
        self.tg_miss = 0
        if t < self.tg_search:
            return
        found, _ = self.api.delay_many([m for m in group.get('all') or [] if m in alive and m != now],
                                       TG_PROBE_URL, timeout_ms=TIMEOUT_MS, http_timeout=HTTP_TIMEOUT,
                                       deadline=HTTP_TIMEOUT + 0.5)
        found = {m: d for m, d in found.items() if d is not None}
        main = (self.proxies.get(GROUP) or {}).get('now')
        self.say('tg', '' if found else f'{TG_GROUP}: Bot API не отвечает ни через один экзит, '
                                        f'повторю через {TG_INTERVAL:.0f}с')
        if found:
            target = main if main in found else min(found, key=found.get)
        else:
            # Стоять на мёртвом или выключенном незачем: живой довезёт бота, когда Bot API вернётся.
            self.tg_search = t + TG_INTERVAL
            if (now in alive and node_of(now) not in self.excluded) or main not in alive or main == now:
                return
            target = main
        self.select(TG_GROUP, target, f'{now or "-"}: {why}')

    def tg_loop(self):
        """Свой поток: тайм-аут Bot API не держит выбор экзита."""
        while True:
            time.sleep(INTERVAL)
            try:
                self.tg()
            except Exception as e:
                print(f'tg error: {e}', file=sys.stderr, flush=True)


def main():
    if not SECRET:
        print('CLASH_SECRET не задан', file=sys.stderr)
        sys.exit(1)
    log(f'failover-watchdog: group={GROUP} interval={INTERVAL}s probe_timeout={TIMEOUT_MS}ms '
        f'dead_after={DEAD_AFTER} tolerance={TOLERANCE}ms latency_hold={LATENCY_HOLD} '
        f'balance={"on" if BALANCE else "off"} warp_return={WARP_RETURN:.0f}s tcp_return={TCP_RETURN:.0f}s '
        f'tg={TG_INTERVAL:.0f}s restart_after={RESTART_AFTER} restart_min={RESTART_MIN_INTERVAL:.0f}s api={CLASH}')
    dog = Watchdog()
    threading.Thread(target=dog.tg_loop, daemon=True).start()
    while True:
        start = clock()
        try:
            dog.cycle()
        except Exception as e:
            print(f'tick error: {e}', file=sys.stderr, flush=True)
        time.sleep(max(0.0, INTERVAL - (clock() - start)))
