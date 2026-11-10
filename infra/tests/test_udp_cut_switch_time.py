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


class TestUdpCutSwitchTime(unittest.TestCase):
    def switch_time(self, w, warm):
        """Время от обрыва UDP до ухода direct на TCP; обрыв через warm с после старта."""
        clash = SlowClash(tf.router_config((NODE,)))
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


if __name__ == '__main__':
    unittest.main()
