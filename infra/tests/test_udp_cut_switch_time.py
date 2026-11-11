"""Обрыв UDP до экзита: канал уходит на TCP за обещанное время.

Обещание docs/failover.md: ожидание проверки, проба и до-проверки, каждая
по тайм-ауту. На стенде выходило дольше: проверка каналов стояла в общем
цикле за проходами, которые платят свои тайм-ауты, TCP пробовался только
после всех до-проверок hy2, а постоянно мёртвый соседний канал (WARP-нога
без выхода) каждый проход заново до-проверялся и держал пробу живых.

Сторож тут идёт как в бою: main() со своими потоками и настоящими часами,
пороги уменьшены. Мёртвая проба отвечает через свой тайм-аут, как clash-api.
"""
import contextlib
import io
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

import test_failover_restart as fr
import test_tcp_fallback as tf

NODE = 'aa'
PROBE_S = 0.6
ENV = {'INTERVAL': '0.2', 'PROBE_TIMEOUT_MS': str(int(PROBE_S * 1000)), 'DEAD_AFTER': '3',
       'CONFIRM_GAP': '0.05', 'TG_INTERVAL_S': '0.2'}
SLACK = 0.6   # запас на планировщик и живые пробы, с


class StopTime:
    """time сторожа: часы настоящие, sleep после stop завершает поток."""

    def __init__(self):
        self.stop = threading.Event()

    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, seconds):
        if self.stop.wait(seconds):
            raise SystemExit


class SlowClash(tf.Clash):
    """Мёртвая проба ждёт тайм-аут; WARP-нога мертва всегда, UDP - с cut()."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.moved = {}
        self.lock = threading.Lock()

    def alive(self, tag):
        return 'warp' not in self.leaf(tag)['tag'] and super().alive(tag)

    def cut(self):
        self.udp.discard(NODE)
        return time.monotonic()

    def req(self, method, path, body=None, **kw):
        if method == 'GET' and path.split('?')[0].endswith('/delay'):
            name = urllib.parse.unquote(path.split('?')[0]).split('/')[2]
            if not self.alive(name):
                time.sleep(PROBE_S)
                raise urllib.error.HTTPError(path, 504, 'timeout', None, None)
            time.sleep(0.02)
            return {'delay': 20}
        with self.lock:
            if method == 'PUT':
                self.moved.setdefault(body['name'], time.monotonic())
            return super().req(method, path, body, **kw)


urllib = tf.urllib


class MidPassClash(SlowClash):
    """WARP-нога жива и отвечает медленнее direct. Обрыв падает внутрь прохода
    проверки каналов: direct ответить успел, warp того же прохода - уже нет."""
    WARP_S = 0.15

    def __init__(self, cfg):
        super().__init__(cfg)
        self.armed = False
        self.cut_at = None

    def alive(self, tag):
        return tf.Clash.alive(self, tag)

    def cut(self):
        self.armed = True
        deadline = time.monotonic() + 5
        while self.cut_at is None and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.cut_at

    def req(self, method, path, body=None, **kw):
        if method == 'GET' and path.split('?')[0].endswith('/delay'):
            name = urllib.parse.unquote(path.split('?')[0]).split('/')[2]
            if name == f'transport-hy2-{NODE}-warp' and NODE in self.udp:
                time.sleep(self.WARP_S)
                if NODE not in self.udp:
                    time.sleep(PROBE_S - self.WARP_S)
                    raise urllib.error.HTTPError(path, 504, 'timeout', None, None)
                return {'delay': int(self.WARP_S * 1000)}
            if name == f'transport-hy2-{NODE}-direct' and self.armed and self.cut_at is None \
                    and NODE in self.udp:
                time.sleep(0.02)
                self.udp.discard(NODE)
                self.cut_at = time.monotonic()
                return {'delay': 20}
        return super().req(method, path, body, **kw)


class SwitchTimeCase(unittest.TestCase):
    CLASH = SlowClash

    def switch_time(self, w, warm):
        """Время от обрыва UDP до ухода direct на TCP; обрыв через warm с после старта."""
        clash = self.clash = self.CLASH(tf.router_config((NODE,)))
        clock, out, restarts, events = StopTime(), io.StringIO(), [], []
        before = threading.active_count()
        with patch.object(w, '_req', clash.req), patch.object(w, 'time', clock), \
                patch.object(w, '_restart_sing_box', side_effect=restarts.append), \
                patch.object(w, '_bot_event', side_effect=lambda *a: events.append(a)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            runner = threading.Thread(target=w.main, daemon=True)
            runner.start()
            time.sleep(warm)
            cut = clash.cut()
            self.assertIsNotNone(cut, 'обрыв не случился\n' + out.getvalue()[-1500:])
            deadline = cut + 12 * PROBE_S
            tcp = f'transport-tcp-{NODE}-direct'
            while tcp not in clash.moved and time.monotonic() < deadline:
                time.sleep(0.02)
            clock.stop.set()
            runner.join(10)
            end = time.monotonic() + 10
            while threading.active_count() > before and time.monotonic() < end:
                time.sleep(0.05)
        self.assertIn(tcp, clash.moved, 'direct не ушёл на TCP\n' + out.getvalue()[-2000:])
        self.assertEqual(restarts, [], 'sing-box перезапущен при живом TCP\n' + out.getvalue()[-2000:])
        self.assertEqual(len([e for e in events if 'недоступен' in e[0]]), 1, events)
        return clash.moved[tcp] - cut, out.getvalue()


class TestUdpCutSwitchTime(SwitchTimeCase):
    def test_dead_neighbour_and_other_passes_do_not_delay_the_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = fr.watchdog(tmp, **ENV)
            limit = w.INTERVAL + w.KNOWN_DEAD_WAIT + w.DEAD_AFTER * PROBE_S \
                + (w.DEAD_AFTER - 1) * w.CONFIRM_GAP + SLACK
            for warm in (4.0, 4.7):
                with self.subTest(warm=warm):
                    took, log = self.switch_time(fr.watchdog(tmp, **ENV), warm)
                    self.assertLessEqual(
                        took, limit, f'до ухода на TCP {took:.1f} с при пределе {limit:.1f} с\n' + log[-1500:])


class TestUdpCutInsideChannelCheck(SwitchTimeCase):
    """Обрыв между ответами direct и warp одного прохода: пока идут до-проверки
    warp, direct не пробовали, и его смерть ждала следующего прохода - два
    прохода подряд, на стенде 21,6 с вместо 10-14."""
    CLASH = MidPassClash

    def test_channel_dying_right_after_its_probe_is_seen_in_the_same_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = fr.watchdog(tmp, **ENV)
            # Остаток первой пробы warp, затем DEAD_AFTER проб direct с паузами.
            limit = PROBE_S + w.DEAD_AFTER * (PROBE_S + w.CONFIRM_GAP) + SLACK
            took, log = self.switch_time(w, 3.0)
            self.assertLessEqual(
                took, limit, f'до ухода на TCP {took:.1f} с при пределе {limit:.1f} с\n' + log[-1500:])
            # Сосед, умерший первым, позднего не ждёт: его проход не растёт.
            warp = self.clash.moved[f'transport-tcp-{NODE}-warp'] - self.clash.cut_at
            self.assertLessEqual(warp, w.DEAD_AFTER * (PROBE_S + w.CONFIRM_GAP) + SLACK / 2,
                                 f'warp ушёл на TCP за {warp:.1f} с\n' + log[-1500:])


if __name__ == '__main__':
    unittest.main()
