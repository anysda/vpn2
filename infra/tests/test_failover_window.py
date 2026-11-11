"""Окно без связи при обрыве UDP до экзита: худшая точка, а не среднее.

Проверка каналов идёт на модельных часах с боевыми порогами: проба отвечает
через задержку канала либо молчит до своего тайм-аута, обрыв задан расписанием.
Обрыв проходит по всем фазам такта проверки, худшая фаза обязана уложиться.
"""
import contextlib
import io
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_failover_restart as fr
import test_tcp_fallback as tf

ENV = {'INTERVAL': '2', 'PROBE_TIMEOUT_MS': '3000', 'DEAD_AFTER': '3', 'CONFIRM_GAP': '0.4'}
NODES = ('aa', 'bb')
START, WARM = 1000.0, 20.0
API_S = 0.02      # ответ clash-api на GET /proxies и PUT
LIVE_S = 0.1      # задержка живой пробы
FOREVER = (0.0, 1e9)
CONFIG = tf.router_config(NODES)


class Clock:
    def __init__(self, until):
        self.mono, self.until = START, until

    def monotonic(self):
        return self.mono

    def time(self):
        return self.mono + 1_800_000_000.0

    def sleep(self, seconds):
        self.mono += seconds
        if self.mono >= self.until:
            raise fr.Stop


class Link(tf.Clash):
    """clash-api по расписанию: cuts - когда нет UDP или TCP до узла,
    noise - когда молчит проба одного канала, slow - задержка его пробы."""

    def __init__(self, clock):
        super().__init__(CONFIG)
        self.clock = clock
        self.cuts, self.noise, self.slow = {}, {}, {}
        self.moves = []

    def spans(self, tag):
        spans = list(self.noise.get(tag, ()))
        leaf = self.leaf(tag)
        if leaf['type'] == 'hysteria2':
            spans += self.cuts.get((tf.node_of(leaf), 'udp'), ())
        elif tf.is_reality(leaf):
            spans += self.cuts.get((tf.node_of(leaf), 'tcp'), ())
        return spans

    def answer(self, tag, start, timeout):
        """(через сколько вернулась проба, задержка в мс или None)."""
        spans, lat, at = self.spans(tag), self.slow.get(tag, LIVE_S), start
        moved = True
        while moved:
            moved = False
            for begin, end in spans:
                if at + lat > begin and at < end:
                    at, moved = end, True
        took = at + lat - start
        return (took, int(lat * 1000)) if took <= timeout else (timeout, None)

    def req(self, method, path, body=None, **kw):
        self.clock.mono += API_S
        if method == 'PUT':
            self.moves.append((self.clock.mono, tf.urllib.parse.unquote(path).split('/')[2], body['name']))
        return super().req(method, path, body, **kw)


class Model:
    """Сторож на модельных часах: пробы считает Link, время двигает только модель."""

    def __init__(self, tmp, seconds):
        self.w = fr.watchdog(tmp, **ENV)
        self.clock = Clock(START + seconds)
        self.link = Link(self.clock)
        self.out = io.StringIO()
        self.events = []

    def probe_all(self, members, _url=None, known_dead=None, recheck=None, **_slow):
        w, start = self.w, self.clock.mono
        timeout = w.TIMEOUT_MS / 1000.0
        hurry = known_dead or ()
        result, wait, quick = {}, 0.0, set()
        for m in members:
            took, delay = self.link.answer(m, start, timeout)
            if delay is not None and took <= w.INTERVAL:
                quick.add(m)
            limit = w.KNOWN_DEAD_WAIT if m in hurry else w.JOIN_DEADLINE
            result[m] = delay if took <= limit else None
            wait = max(wait, min(took, limit))
        if recheck and any(self.link.answer(m, start, timeout)[0] > w.INTERVAL for m in members if m not in hurry):
            # Опрос ждёт молчащего дольше такта: ответившие получают вторую пробу.
            for m in recheck[0]:
                if m in quick and m not in hurry:
                    took, recheck[1][m] = self.link.answer(m, start + w.INTERVAL, timeout)
                    wait = max(wait, w.INTERVAL + took)
        self.clock.mono = start + wait
        return result, {}

    def run(self):
        w = self.w
        with patch.object(w, '_req', self.link.req), patch.object(w, '_probe_all', self.probe_all), \
                patch.object(w, 'time', self.clock), \
                patch.object(w, '_bot_event', side_effect=lambda *a: self.events.append(a)), \
                contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.out):
            try:
                w._transport_loop()
            except fr.Stop:
                pass
        return self

    def moved(self, channel, to):
        """Когда канал channel ушёл на to (hy2 или tcp); None - не уходил."""
        times = [at for at, name, target in self.link.moves
                 if name == channel and target.startswith(f'transport-{to}-')]
        return times[0] if times else None

    def moves_of(self, channel):
        return [target.split('-')[1] for _, name, target in self.link.moves if name == channel]


def warp_tags(node):
    return [f'transport-{leg}-{node}-warp' for leg in ('hy2', 'tcp')]


def profiles(cut):
    """Соседние каналы при обрыве: чем они могут задержать уход direct."""
    return {
        'warp жив': {},
        'warp мёртв всегда': {t: [FOREVER] for n in NODES for t in warp_tags(n)},
        'TCP-нога warp шумит с обрыва': {'transport-tcp-aa-warp': [(cut, cut + 30)]},
        'TCP-нога warp шумит и до обрыва': {'transport-tcp-aa-warp': [(cut - 1, cut + 30)]},
        'hy2 warp соседнего узла мигнул перед обрывом': {'transport-hy2-bb-warp': [(cut - 3.2, cut - 0.1)]},
    }


def phases(step=0.05, span=2.6):
    """Обрыв в каждой фазе такта: такт - INTERVAL плюс сам проход."""
    return [round(i * step, 2) for i in range(int(span / step) + 1)]


class UdpCutWindowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def model(self, seconds, cuts, noise=None, slow=None):
        model = Model(self.tmp, seconds)
        model.link.cuts, model.link.noise, model.link.slow = cuts, noise or {}, slow or {}
        return model.run()

    def bound(self, w):
        """Ожидание такта, DEAD_AFTER проб по тайм-ауту с паузами, проба TCP и PUT."""
        return w.INTERVAL + w.DEAD_AFTER * w.TIMEOUT_MS / 1000.0 + (w.DEAD_AFTER - 1) * w.CONFIRM_GAP + 0.7

    def test_bound_leaves_the_client_its_fifteen_seconds(self):
        # Клиент замечает уход ещё через свой повтор запроса - на стенде до 2,2 с.
        self.assertLessEqual(self.bound(fr.watchdog(self.tmp, **ENV)) + 2.2, 15.0)

    def test_cut_at_every_phase_moves_direct_to_tcp_in_time(self):
        worst = {}
        for phase in phases():
            cut = START + WARM + phase
            for name, noise in profiles(cut).items():
                run = self.model(WARM + phase + 25, {('aa', 'udp'): [(cut, cut + 120)]}, noise)
                at = run.moved('transport-aa-direct', 'tcp')
                took = None if at is None else at - cut
                if took is None or took > worst.get(name, (0, 0))[0]:
                    worst[name] = (took if took is not None else 1e9, phase)
        limit = self.bound(fr.watchdog(self.tmp, **ENV))
        late = {name: v for name, v in worst.items() if v[0] > limit}
        self.assertEqual(late, {}, f'предел {limit:.1f} с; худшее (с, фаза): {worst}')

    def test_slow_warp_probe_does_not_hide_the_cut(self):
        # Проба warp отвечает медленнее direct: обрыв попадает между их ответами.
        slow = {t: 0.8 for n in NODES for t in warp_tags(n)}
        limit = self.bound(fr.watchdog(self.tmp, **ENV))
        for phase in phases(0.1, 3.4):
            cut = START + WARM + phase
            run = self.model(WARM + phase + 25, {('aa', 'udp'): [(cut, cut + 120)]}, slow=slow)
            at = run.moved('transport-aa-direct', 'tcp')
            self.assertIsNotNone(at, phase)
            self.assertLessEqual(at - cut, limit, f'фаза {phase}')

    def test_both_exits_cut_at_once(self):
        limit = self.bound(fr.watchdog(self.tmp, **ENV))
        for phase in phases(0.1):
            cut = START + WARM + phase
            span = [(cut, cut + 120)]
            run = self.model(WARM + phase + 25, {('aa', 'udp'): span, ('bb', 'udp'): span})
            for node in NODES:
                at = run.moved(f'transport-{node}-direct', 'tcp')
                self.assertIsNotNone(at, f'{node}, фаза {phase}')
                self.assertLessEqual(at - cut, limit, f'{node}, фаза {phase}')
            self.assertEqual(len([e for e in run.events if 'недоступен' in e[0]]), 2, run.events)

    def test_five_second_cut_moves_nothing(self):
        for phase in phases(0.1):
            cut = START + WARM + phase
            for name, noise in profiles(cut).items():
                run = self.model(WARM + phase + 40, {('aa', 'udp'): [(cut, cut + 5)]}, noise)
                moves = [m for m in run.link.moves if '-aa-direct' in m[1] or '-aa-mgmt' in m[1]]
                self.assertEqual(moves, [], f'{name}, фаза {phase}')
                self.assertEqual(run.events, [], f'{name}, фаза {phase}')

    def test_flapping_udp_does_not_ping_pong(self):
        w = fr.watchdog(self.tmp, **ENV)
        # Короткие провалы: ни одного ухода.
        short = [(START + WARM + i * 8, START + WARM + i * 8 + 4) for i in range(12)]
        run = self.model(WARM + 120, {('aa', 'udp'): short})
        self.assertEqual(run.moves_of('transport-aa-direct'), [])
        # Длинные провалы с паузами короче окна возврата: один уход, обратно -
        # только после TCP_RETURN ровной работы.
        long = [(START + WARM + i * 32, START + WARM + i * 32 + 12) for i in range(6)]
        calm = long[-1][1]
        run = self.model(WARM + 6 * 32 + w.TCP_RETURN + 40, {('aa', 'udp'): long})
        self.assertEqual(run.moves_of('transport-aa-direct'), ['tcp', 'hy2'])
        back = run.moved('transport-aa-direct', 'hy2')
        self.assertGreaterEqual(back - calm, w.TCP_RETURN - w.INTERVAL - 1)
        self.assertEqual(len(run.events), 2, run.events)


class MainModel(Model):
    """Общий цикл сторожа на модельных часах; проверка каналов выключена.
    Bot API молчит через все выходы: его обход стоит цикла полного тайм-аута."""

    def probe_all(self, members, url=None, known_dead=None, **rest):
        w = self.w
        if url != w.TG_PROBE_URL:
            return super().probe_all(members, url, known_dead, **rest)
        patient = [m for m in members if m not in (known_dead or ())]
        self.clock.mono += w.TIMEOUT_MS / 1000.0 if patient else w.KNOWN_DEAD_WAIT
        return {m: None for m in members}, {m: 'timed out' for m in members}

    def probe(self, tag, out, errs=None, _url=None):
        """До-проверка выбранного выхода: одна проба, ждут её целиком.
        Проба запасной ноги идёт своим потоком рядом и времени цикла не стоит."""
        took, out[tag] = self.link.answer(tag, self.clock.mono, self.w.TIMEOUT_MS / 1000.0)
        if threading.current_thread() is threading.main_thread():
            self.clock.mono += took
        if out[tag] is None and errs is not None:
            errs[tag] = 'timed out'

    def run(self):
        w = self.w
        with patch.object(w, '_req', self.link.req), patch.object(w, '_probe_all', self.probe_all), \
                patch.object(w, '_probe', self.probe), \
                patch.object(w, 'time', self.clock), patch.object(w, '_transport_loop', lambda: None), \
                patch.object(w, '_restart_sing_box', create=True), \
                patch.object(w, '_bot_event', side_effect=lambda *a, **_k: self.events.append(a)), \
                contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.out):
            try:
                w.main()
            except fr.Stop:
                pass
        return self


class ExitDeathWindowTests(unittest.TestCase):
    LIMIT = 14.0    # docs/failover.md: смерть экзита - переезд за 10-14 с

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def death(self, phase, warm=60.0):
        """Экзит aa умер целиком (UDP и TCP) в фазе phase; когда выбор ушёл на bb."""
        cut = START + warm + phase
        span = [(cut, cut + 120)]
        model = MainModel(self.tmp, warm + phase + 30)
        model.link.cuts = {('aa', 'udp'): span, ('aa', 'tcp'): span}
        model.run()
        w = model.w
        self.assertIn('Bot API не отвечает ни через один экзит', model.out.getvalue())
        moves = [at for at, name, target in model.link.moves if name == w.GROUP and '-bb-' in target]
        return (moves[0] - cut) if moves else None

    def test_death_at_every_phase_switches_in_documented_time(self):
        # Окно шире периода обхода Bot API: обрыв попадает и на сам обход.
        worst = (0.0, None)
        for i in range(int(34 / 0.1) + 1):
            phase = round(i * 0.1, 1)
            took = self.death(phase)
            self.assertIsNotNone(took, f'фаза {phase}')
            worst = max(worst, (took, phase))
        self.assertLessEqual(worst[0], self.LIMIT, f'худшая фаза {worst[1]} с')


if __name__ == '__main__':
    unittest.main()
