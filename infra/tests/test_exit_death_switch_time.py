"""Смерть текущего экзита при постоянно мёртвом соседнем выходе: срок переключения.

Обещание docs/failover.md: ожидание проверки, проба и до-проверки, каждая
по тайм-ауту. Постоянно мёртвый сосед (WARP-нога без выхода) каждый проход
держал опрос экзитов свой тайм-аут: проверка шла реже, и выбор экзита
запаздывал на чужие тайм-ауты.

Сторож идёт как в бою: main() со своими потоками и настоящими часами, пороги
уменьшены. Мёртвая проба отвечает через свой тайм-аут, как clash-api.
"""
import contextlib
import io
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_failover_restart as fr
import test_tcp_fallback as tf
import test_udp_cut_switch_time as ut

NODES = ('aa', 'bb')
ENV = {**ut.ENV, 'DEAD_AFTER': '2'}
SLACK = 0.3    # запас на планировщик и живые пробы, с


class DeadWarpClash(ut.SlowClash):
    """WARP-ноги мертвы всегда; kill() роняет узел целиком."""

    def __init__(self, cfg, group):
        super().__init__(cfg)
        self.group, self.switched = group, []

    def kill(self, node):
        self.udp.discard(node)
        self.tcp.discard(node)
        return time.monotonic()

    def req(self, method, path, body=None, **kw):
        if method == 'PUT' and tf.urllib.parse.unquote(path).endswith('/' + self.group):
            self.switched.append((time.monotonic(), body['name']))
        return super().req(method, path, body, **kw)


class TestExitDeathSwitchTime(unittest.TestCase):
    def switch_time(self, w, warm):
        """Время от смерти узла текущего экзита до ухода выбора на живой узел."""
        clash = DeadWarpClash(tf.router_config(NODES), w.GROUP)
        clock, out, restarts = ut.StopTime(), io.StringIO(), []
        before = threading.active_count()
        with patch.object(w, '_req', clash.req), patch.object(w, 'time', clock), \
                patch.object(w, '_restart_sing_box', side_effect=restarts.append), \
                patch.object(w, '_bot_event'), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            runner = threading.Thread(target=w.main, daemon=True)
            runner.start()
            time.sleep(warm)
            node = w._node_of(clash.now[w.GROUP])
            self.assertEqual(clash.switched, [], 'выбор гулял до смерти узла\n' + out.getvalue()[-1500:])
            died = clash.kill(node)
            deadline = died + 12 * ut.PROBE_S
            while not clash.switched and time.monotonic() < deadline:
                time.sleep(0.02)
            clock.stop.set()
            runner.join(10)
            end = time.monotonic() + 10
            while threading.active_count() > before and time.monotonic() < end:
                time.sleep(0.05)
        log = out.getvalue()
        self.assertTrue(clash.switched, 'выбор не ушёл с мёртвого узла\n' + log[-2000:])
        at, target = clash.switched[0]
        self.assertNotEqual(w._node_of(target), node, log[-1500:])
        self.assertNotIn('warp', target, log[-1500:])
        self.assertEqual(restarts, [], 'sing-box перезапущен при живом соседнем узле\n' + log[-1500:])
        return at - died, log

    def test_dead_neighbour_does_not_delay_the_exit_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = fr.watchdog(tmp, **ENV)
            # Учтённо мёртвых ждут коротко два прохода цикла: опрос экзитов и поиск зависших.
            limit = w.INTERVAL + 2 * w.KNOWN_DEAD_WAIT + w.DEAD_AFTER * ut.PROBE_S \
                + (w.DEAD_AFTER - 1) * w.CONFIRM_GAP + SLACK
            for warm in (4.0, 4.3, 4.6, 4.9):
                with self.subTest(warm=warm):
                    took, log = self.switch_time(fr.watchdog(tmp, **ENV), warm)
                    self.assertLessEqual(
                        took, limit, f'до смены экзита {took:.2f} с при пределе {limit:.2f} с\n' + log[-1500:])


if __name__ == '__main__':
    unittest.main()
