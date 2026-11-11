"""Обрыв UDP: перезапуск sing-box не спорит с уходом каналов на TCP.

Проверка каналов идёт своим потоком, счётчик «мертвы все выходы» - в общем
цикле. Порядок событий между ними здесь задаёт тест, а не фаза часов:

- третья мёртвая проверка общего цикла запущена до переключения каналов на
  TCP, а её итог пришёл после (пробы шли ещё по hy2);
- три мёртвые проверки набрались раньше, чем проверка каналов вынесла вердикт.

В обоих случаях запасной TCP жив, и перезапуск sing-box рвёт соединения зря.
Мертвы обе ноги - перезапуск как раньше.
"""
import contextlib
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_failover_restart as fr
import test_tcp_fallback as tf


class RaceClash(tf.Clash):
    """Пробы выходов можно «заморозить»: итог - по состоянию на запуск пробы,
    а пока проба в пути, выполняется hook (проход проверки каналов)."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.frozen = {}
        self.hook = None
        self.lock = threading.Lock()

    def freeze(self, tags, hook):
        self.frozen = {t: self.alive(t) for t in tags}
        self.hook = hook

    def req(self, method, path, body=None, **kw):
        name = tf.urllib.parse.unquote(path.split('?')[0]).split('/')[2] \
            if path.endswith('/delay') or '/delay?' in path else ''
        if name in self.frozen:
            with self.lock:
                hook, self.hook = self.hook, None
            if hook:
                hook()
            if self.frozen[name]:
                return {'delay': 100}
            raise tf.urllib.error.URLError(tf.socket.timeout('timed out'))
        return super().req(method, path, body, **kw)


class RaceCase(unittest.TestCase):
    EXITS = ('hy2-aa-direct', 'hy2-aa-warp')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.w = fr.watchdog(tmp.name)
        self.clash = RaceClash(tf.router_config(('aa',)))
        self.run_ = fr.Run(self.w, self.clash, fr.Clock(self.w.INTERVAL))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        w, run = self.w, self.run_
        for target, name, value in (
                (w, '_req', self.clash.req), (w.time, 'monotonic', run.clock.monotonic),
                (w.time, 'time', run.clock.time), (w.time, 'sleep', lambda _s: None)):
            stack.enter_context(patch.object(target, name, value))
        stack.enter_context(patch.object(w, '_restart_sing_box', side_effect=run.restart))
        stack.enter_context(patch.object(w, '_bot_event', side_effect=run.event))
        stack.enter_context(contextlib.redirect_stdout(run.out))
        stack.enter_context(contextlib.redirect_stderr(run.out))

    def check(self, n=1):
        """Проверка общего цикла: замер выходов и счётчик перезапуска."""
        for _ in range(n):
            self.run_.clock.mono += self.w.INTERVAL
            self.w.tick()
            self.w.restart_tick()

    def channels(self):
        self.w._transport_pass()

    def log(self):
        return self.run_.out.getvalue()[-2000:]

    def on_tcp(self):
        return all(tf.is_reality(self.clash.leaf(t)) for t in self.EXITS)


class TestSwitchRace(RaceCase):
    def test_switch_lands_inside_third_dead_check(self):
        self.check()
        self.clash.udp.discard('aa')
        self.check(2)
        # третья проверка: пробы ушли по hy2, каналы переключились, пока они в пути
        self.clash.freeze(self.EXITS, self.channels)
        self.check()
        self.clash.frozen = {}
        self.assertTrue(self.on_tcp(), 'каналы не ушли на TCP\n' + self.log())
        self.assertEqual(self.run_.restarts, [],
                         'каналы уже на TCP, а sing-box перезапущен\n' + self.log())
        self.check(5)
        self.assertEqual(self.run_.restarts, [], self.log())

    def test_three_dead_checks_before_channel_verdict(self):
        self.check()
        self.clash.udp.discard('aa')
        self.check(4)
        self.assertEqual(self.run_.restarts, [],
                         'запасной TCP жив, проверка каналов ещё не решила - '
                         'перезапуск лишний\n' + self.log())
        self.channels()
        self.assertTrue(self.on_tcp(), self.log())
        self.check(5)
        self.assertEqual(self.run_.restarts, [], self.log())

    def test_tcp_dies_after_switch_counts_from_zero(self):
        self.check()
        self.clash.udp.discard('aa')
        self.check(2)
        self.clash.freeze(self.EXITS, self.channels)
        self.check()
        self.clash.frozen = {}
        self.clash.tcp.discard('aa')
        self.check(2)
        self.assertEqual(self.run_.restarts, [],
                         'после переключения счёт мёртвых проверок с нуля\n' + self.log())
        self.check()
        self.assertEqual(len(self.run_.restarts), 1, self.log())

    def test_both_legs_dead_restart_on_third_check(self):
        self.check()
        self.clash.udp.discard('aa')
        self.clash.tcp.discard('aa')
        self.check(2)
        self.assertEqual(self.run_.restarts, [], self.log())
        self.check()
        self.assertEqual(len(self.run_.restarts), 1,
                         'мертвы hy2 и TCP - перезапуск на третьей проверке\n' + self.log())


class TestPinSwitchRace(RaceCase):
    """Тот же обрыв UDP глазами ручных маршрутов: запасной TCP жив - узел не
    идёт в штрафную, pin не уходит со своего выхода."""
    PIN = 'pin-hy2-aa-direct'

    def check(self, n=1):
        for _ in range(n):
            super().check()
            self.w.pins_tick(self.clash.req('GET', '/proxies')['proxies'])

    def pin_now(self):
        return self.clash.req('GET', '/proxies')['proxies'][self.PIN]['now']

    def assert_untouched(self):
        self.assertNotIn('в штрафной', self.log())
        self.assertNotIn('foreign-best', self.log())
        self.assertEqual(self.pin_now(), 'hy2-aa-direct', self.log())

    def test_switch_lands_inside_last_miss(self):
        self.check()
        self.clash.udp.discard('aa')
        self.check(self.w.DEAD_AFTER - 1)
        self.clash.freeze(self.EXITS, self.channels)
        self.check()
        self.clash.frozen = {}
        self.assertTrue(self.on_tcp(), self.log())
        self.assert_untouched()
        self.check(5)
        self.assert_untouched()

    def test_misses_before_channel_verdict(self):
        self.check()
        self.clash.udp.discard('aa')
        self.check(self.w.DEAD_AFTER + 1)
        self.assert_untouched()
        self.channels()
        self.check(5)
        self.assert_untouched()

    def test_tcp_dies_after_switch_counts_from_zero(self):
        self.check()
        self.clash.udp.discard('aa')
        self.check(self.w.DEAD_AFTER - 1)
        self.clash.freeze(self.EXITS, self.channels)
        self.check()
        self.clash.frozen = {}
        self.clash.tcp.discard('aa')
        self.check(self.w.DEAD_AFTER - 1)
        self.assert_untouched()
        self.check()
        self.assertIn('в штрафной', self.log())
        self.assertNotEqual(self.pin_now(), 'hy2-aa-direct', self.log())

    def test_both_legs_dead_pin_leaves_on_last_miss(self):
        self.check()
        self.clash.udp.discard('aa')
        self.clash.tcp.discard('aa')
        self.check(self.w.DEAD_AFTER - 1)
        self.assert_untouched()
        self.check()
        self.assertIn('в штрафной', self.log())
        self.assertNotEqual(self.pin_now(), 'hy2-aa-direct', self.log())


if __name__ == '__main__':
    unittest.main()
