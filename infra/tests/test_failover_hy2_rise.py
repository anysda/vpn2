"""После полной аварии entry → экзит hy2 поднимается дольше TCP - это не обрыв UDP.

Связь до экзита пропала целиком (UDP и TCP), сторож перезапустил sing-box.
Связь вернулась: TCP соединяется сразу, Hysteria2 (QUIC) - через пару секунд.
Первая проверка видит hy2 мёртвым и TCP живым. Уводить каналы на TCP и слать
«UDP недоступен» тут нельзя: через минуту второе событие «снова работает», и
клиенты всю минуту идут через TCP. Настоящая резка UDP (hy2 мёртв дольше
окна подъёма) уходит на TCP, как раньше.

clash-api - модель из test_tcp_fallback по настоящему конфигу генератора;
лист hysteria2 из rise мёртв, пока часы не дошли до заданного момента.
"""
import tempfile
import unittest

import test_tcp_fallback as tf
import test_failover_restart as fr


KINDS = ('direct', 'warp')


class RiseClash(tf.Clash):
    def __init__(self, cfg, clock):
        super().__init__(cfg)
        self.clock = clock
        self.rise = {}   # лист hysteria2 -> monotonic, с которого он отвечает

    def alive(self, tag):
        leaf = self.leaf(tag)
        if self.clock.mono < self.rise.get(leaf.get('tag'), 0):
            return False
        return super().alive(tag)


class TestHy2RiseAfterOutage(unittest.TestCase):
    def start(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        w = fr.watchdog(tmp.name)
        clock = fr.Clock(w.INTERVAL)
        clash = RiseClash(tf.router_config(('aa',)), clock)
        run = fr.Run(w, clash, clock)
        run.go(3)
        for kind in KINDS:
            self.assertEqual(clash.leaf(f'hy2-aa-{kind}')['type'], 'hysteria2', self.log(run))
        return w, clash, clock, run

    def log(self, run):
        return '\n' + run.out.getvalue()[-2500:]

    def outage(self, w, clash, run, restart=True):
        """Всё мертво до перезапуска sing-box (или столько же, если перезапуск
        запрещён частотой) и ещё несколько проверок после."""
        before = len(run.restarts)
        clash.udp.discard('aa')
        clash.tcp.discard('aa')
        run.go(w.RESTART_AFTER_CHECKS + 5)
        self.assertEqual(len(run.restarts), before + (1 if restart else 0),
                         'перезапуск sing-box не тот' + self.log(run))

    def link_back(self, clash, clock, slow, rise_s, udp=True):
        """Связь вернулась: TCP сразу, hy2 из slow - через rise_s секунд."""
        clash.tcp.add('aa')
        if udp:
            clash.udp.add('aa')
        for kind in slow:
            clash.rise[f'transport-hy2-aa-{kind}'] = clock.mono + rise_s

    def after(self, run, mark_out, mark_ev):
        out = run.out.getvalue()[mark_out:]
        moves = [ln for ln in out.splitlines() if '-> transport-tcp-' in ln]
        return moves, tf.udp_events(run)[mark_ev:]

    def assert_no_false_tcp(self, w, clash, clock, run, slow, rise_s):
        mark_out, mark_ev = len(run.out.getvalue()), len(tf.udp_events(run))
        self.link_back(clash, clock, slow, rise_s)
        run.go(int((w.TCP_RETURN + 30) / w.INTERVAL))
        moves, events = self.after(run, mark_out, mark_ev)
        self.assertEqual(moves, [], f'hy2 {slow} поднимался {rise_s} с после возврата связи, '
                                    'а каналы ушли на TCP' + self.log(run))
        self.assertEqual(events, [], 'ложное событие про UDP после возврата связи'
                         + self.log(run))
        for kind in KINDS:
            self.assertEqual(clash.leaf(f'hy2-aa-{kind}')['type'], 'hysteria2',
                             f'{kind} не на hy2' + self.log(run))

    def test_a_restart_then_hy2_rises_no_tcp_no_event(self):
        """(а) все мертвы → перезапуск → hy2 мёртв 1-2 проверки, TCP жив → hy2 жив."""
        for checks in (1, 2):
            with self.subTest(checks=checks):
                w, clash, clock, run = self.start()
                self.outage(w, clash, run)
                self.assert_no_false_tcp(w, clash, clock, run, KINDS, checks * w.INTERVAL)

    def test_b_real_udp_cut_after_restart_goes_to_tcp(self):
        """(б) после перезапуска UDP так и не вернулся: уход на TCP за окно подъёма
        и одно событие."""
        w, clash, clock, run = self.start()
        self.outage(w, clash, run)
        mark_out, mark_ev = len(run.out.getvalue()), len(tf.udp_events(run))
        t0 = clock.mono
        self.link_back(clash, clock, (), 0, udp=False)
        took = None
        for _ in range(int(60 / w.INTERVAL)):
            run.go(1)
            if took is None and tf.is_reality(clash.leaf('hy2-aa-direct')):
                took = clock.mono - t0
        window = getattr(w, 'HY2_RISE', 0.0)
        self.assertIsNotNone(took, 'UDP резан после перезапуска, а direct не на TCP'
                             + self.log(run))
        self.assertLessEqual(took, window + w.DEAD_AFTER * w.INTERVAL + 1,
                             f'direct на TCP через {took:.0f} с, окно подъёма {window:.0f} с'
                             + self.log(run))
        for kind in KINDS:
            self.assertTrue(tf.is_reality(clash.leaf(f'hy2-aa-{kind}')), kind + self.log(run))
        _, events = self.after(run, mark_out, mark_ev)
        self.assertEqual(len(events), 1, f'ждём одно событие про UDP: {events}' + self.log(run))
        self.assertIn('недоступен', events[0])

    def test_c_mirror_direct_and_warp_leg(self):
        """(в) медленный подъём только одной ноги узла - direct или warp."""
        for kind in KINDS:
            with self.subTest(kind=kind):
                w, clash, clock, run = self.start()
                self.outage(w, clash, run)
                self.assert_no_false_tcp(w, clash, clock, run, (kind,), 2 * w.INTERVAL)

    def test_d_udp_cut_without_restart_as_before(self):
        """(г) hy2 был жив, потом мёртв DEAD_AFTER проб, TCP жив - на TCP за
        DEAD_AFTER проверок, одно событие (VPN2-651)."""
        w, clash, clock, run = self.start()
        mark_ev = len(tf.udp_events(run))
        clash.udp.discard('aa')
        took = None
        for k in range(1, 11):
            run.go(1)
            if took is None and tf.is_reality(clash.leaf('hy2-aa-direct')):
                took = k
        self.assertIsNotNone(took, 'direct не ушёл на TCP' + self.log(run))
        self.assertLessEqual(took, w.DEAD_AFTER, f'direct на TCP через {took} проверок'
                             + self.log(run))
        self.assertEqual(run.restarts, [])
        self.assertEqual(len(self.after(run, 0, mark_ev)[1]), 1, self.log(run))

    def test_d_udp_cut_long_after_recovered_restart_as_before(self):
        """(г) после перезапуска hy2 поднялся, позже UDP резан - сразу, без окна."""
        w, clash, clock, run = self.start()
        self.outage(w, clash, run)
        self.link_back(clash, clock, KINDS, w.INTERVAL)
        run.go(10)
        clash.udp.discard('aa')
        took = None
        for k in range(1, 11):
            run.go(1)
            if took is None and tf.is_reality(clash.leaf('hy2-aa-direct')):
                took = k
        self.assertIsNotNone(took, 'direct не ушёл на TCP' + self.log(run))
        self.assertLessEqual(took, w.DEAD_AFTER, f'direct на TCP через {took} проверок'
                             + self.log(run))

    def test_e_two_outages_in_a_row_no_false_events(self):
        """(д) две аварии подряд, вторая без перезапуска (предел частоты): на каждую
        ноль ложных событий."""
        w, clash, clock, run = self.start()
        self.outage(w, clash, run)
        self.assert_no_false_tcp(w, clash, clock, run, KINDS, 2 * w.INTERVAL)
        self.outage(w, clash, run, restart=False)
        self.assert_no_false_tcp(w, clash, clock, run, KINDS, 2 * w.INTERVAL)


if __name__ == '__main__':
    unittest.main()
