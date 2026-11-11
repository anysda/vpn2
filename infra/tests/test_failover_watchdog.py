"""Сторож экзитов: то, что обещает docs/failover.md, на виртуальных часах.

Стенд подменяет clash-api и часы; пробы приходят в Health.record так же, как от
потоков проб: выход отвечает, пока жив он сам и выбранная нога его канала.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vpn2/src'))
from vpn2.watchdog import failover as fo, failover_env  # noqa: E402

KINDS = ('direct', 'warp')


class Stand:
    def __init__(self, nodes=('aa', 'bb'), tcp=True, lanes=4):
        self.t, self.down, self.ms, self.conns = 1000.0, set(), {}, []
        self.events, self.restarts, self.journal, self.closed, self.tg_ok = [], [], [], [], None
        exits = [f'hy2-{n}-{k}' for k in KINDS for n in nodes]
        p = self.p = {e: {'type': 'Hysteria2'} for e in exits}

        def selector(name, members):
            p[name] = {'type': 'Selector', 'all': list(members), 'now': members[0]}
        for n in nodes:
            for k in KINDS + ('mgmt',):
                legs = [f'transport-hy2-{n}-{k}', f'transport-tcp-{n}-{k}']
                for leg in legs if tcp else ():
                    p[leg] = {'type': 'Direct'}
                if tcp:
                    selector(f'hy2-{n}-mgmt' if k == 'mgmt' else f'transport-{n}-{k}', legs)
        for name in ['foreign-best', 'tg-best'] + [f'lane-{i:02d}' for i in range(1, lanes + 1)]:
            selector(name, exits)
        for e in exits:
            others = [x for x in exits if x.endswith('-warp') and x != e]
            selector(f'pin-{e}', [e] + (others if e.endswith('-warp') else ['foreign-best']))
        self.tmp = tempfile.TemporaryDirectory()
        fo.clock = lambda: self.t
        fo.log = self.journal.append
        fo.bot_event = lambda subject, detail: self.events.append((subject, detail))
        fo.restart_sing_box = lambda: self.restarts.append(self.t) or ''
        fo.entry_pressure = lambda: ''
        fo._store = lambda name, data: None
        fo._stored = lambda name: {}
        fo.DISABLED_FILE = os.path.join(self.tmp.name, 'exits-disabled.json')
        self.dog = fo.Watchdog(api=self)
        self.dog.health.watch = lambda tags: setattr(self.dog.health, 'want', set(tags))

    # clash-api
    def proxies(self, timeout=None):
        return self.p

    def connections(self, timeout=None):
        return self.conns

    def select(self, group, name, timeout=None):
        assert name in self.p[group]['all'], (group, name)
        self.p[group]['now'] = name

    def close(self, conn_id, timeout=None):
        self.closed.append(conn_id)

    def delay(self, tag, url, **_):
        return (40, '') if self.tg_ok is None or tag in self.tg_ok else (None, 'таймаут')

    def delay_many(self, tags, url, **_):
        return {t: self.delay(t, url)[0] for t in tags}, {}

    # стенд
    def answers(self, tag):
        if tag in self.down:
            return False
        channel = self.p.get(f'transport-{tag[4:]}') if tag.startswith('hy2-') else None
        return channel is None or channel['now'] not in self.down

    def tick(self, seconds=fo.INTERVAL):
        for _ in range(max(1, round(seconds / fo.INTERVAL))):
            for tag in sorted(self.dog.health.want):
                ok = self.answers(tag)
                self.dog.health.record(tag, self.ms.get(tag, 50) if ok else None, '' if ok else 'таймаут')
            self.dog.cycle()
            self.t += fo.INTERVAL

    def kill(self, node, on=True):
        tags = {t for t in self.p if t.split('-')[-2:-1] == [node] and not t.startswith(('pin-', 'transport-' + node))}
        self.down = self.down | tags if on else self.down - tags

    def cut_udp(self, node, on=True):
        tags = {f'transport-hy2-{node}-{k}' for k in KINDS}
        self.down = self.down | tags if on else self.down - tags

    def disable(self, *nodes):
        Path(fo.DISABLED_FILE).write_text(json.dumps({'disabled': list(nodes)}))

    def now(self, group='foreign-best'):
        return self.p[group]['now']

    def said(self, text):
        return [line for line in self.journal if text in line]


class Base(unittest.TestCase):
    def stand(self, **kw):
        saved = {k: getattr(fo, k) for k in ('clock', 'log', 'bot_event', 'restart_sing_box', 'entry_pressure',
                                             '_store', '_stored', 'DISABLED_FILE', 'BALANCE', 'RESTART_AFTER')}
        self.addCleanup(lambda: [setattr(fo, k, v) for k, v in saved.items()])
        s = Stand(**kw)
        self.addCleanup(s.tmp.cleanup)
        s.tick(10)
        self.assertEqual(s.now(), 'hy2-aa-direct')
        return s


class DeadExit(Base):
    def test_replaced_within_documented_bound(self):
        s = self.stand()
        s.kill('aa')
        died = s.t
        while s.now() == 'hy2-aa-direct' and s.t - died < 30:
            s.tick()
        # Потоки проб: до DEAD_AFTER×(тайм-аут 3,5 с) + цикл; docs/failover.md: 10-14 с.
        self.assertLessEqual(s.t - died + fo.DEAD_AFTER * (fo.HTTP_TIMEOUT - fo.INTERVAL), 14.0)
        self.assertEqual(s.now(), 'hy2-bb-direct')
        self.assertEqual(len(s.said('switch -> hy2-bb-direct')), 1)
        self.assertIn('узел aa в штрафной 60с', s.said('switch ->')[0])
        self.assertEqual(s.restarts, [])

    def test_no_ping_pong_on_flapping_exit(self):
        s = self.stand()
        for _ in range(20):
            s.kill('aa')
            s.tick(20)
            s.kill('aa', on=False)
            s.tick(20)
        self.assertEqual(len(s.said('switch ->')), 1)
        self.assertEqual(s.now(), 'hy2-bb-direct')
        s.tick(900)
        self.assertEqual(s.now(), 'hy2-bb-direct', 'ожил - назад не возвращаемся')

    def test_penalty_doubles_on_recurrence(self):
        s = self.stand()
        s.kill('aa')
        s.tick(20)
        s.kill('aa', on=False)
        s.tick(70)
        s.kill('bb')
        s.tick(20)
        self.assertEqual(s.now(), 'hy2-aa-direct')
        s.kill('bb', on=False)
        s.kill('aa')
        s.tick(20)
        self.assertIn('узел aa в штрафной 120с', s.said('switch ->')[-1])

    def test_dead_exit_of_alive_node_is_not_penalized(self):
        s = self.stand()
        s.down.add('hy2-aa-direct')
        s.tick(20)
        self.assertIn('узла aa жив, узел не штрафую', s.said('switch ->')[0])

    def test_faster_exit_needs_stable_advantage(self):
        s = self.stand()
        s.ms['hy2-aa-direct'] = 400
        s.tick(fo.INTERVAL * (fo.LATENCY_HOLD - 1))
        s.ms['hy2-aa-direct'] = 50
        s.tick(20)
        self.assertEqual(s.now(), 'hy2-aa-direct', 'всплеск короче LATENCY_HOLD не повод')
        s.ms['hy2-aa-direct'] = 50 + fo.TOLERANCE + 30
        s.tick(fo.INTERVAL * (fo.LATENCY_HOLD + 1))
        self.assertNotEqual(s.now(), 'hy2-aa-direct')
        self.assertIn('устойчиво', s.said('switch ->')[0])

    def test_all_dead_restarts_sing_box_with_limit(self):
        s = self.stand()
        s.kill('aa')
        s.kill('bb')
        s.tick(40)
        self.assertEqual(len(s.restarts), 1)
        self.assertEqual(s.events, [('Экзиты', 'Все каналы мертвы 3 проверки подряд - перезапустил sing-box')])
        self.assertTrue(s.said('все экзиты не отвечают, выбор не трогаю'))
        s.tick(fo.RESTART_MIN_INTERVAL - 60)
        self.assertEqual(len(s.restarts), 1, 'не чаще RESTART_MIN_INTERVAL_S')
        s.tick(120)
        self.assertEqual(len(s.restarts), 2)

    def test_restart_disabled_by_zero_and_entry_pressure_named(self):
        s = self.stand()
        fo.RESTART_AFTER = 0
        s.kill('aa')
        s.kill('bb')
        s.tick(60)
        self.assertEqual(s.restarts, [])
        fo.RESTART_AFTER = 3
        fo.entry_pressure = lambda: 'память; PSI some avg10: память 35.0%'
        s.tick(10)
        self.assertEqual(s.events[0][0], 'Entry')
        self.assertRegex(s.events[0][1], r'^entry перегружен \(память; .*топ RSS: .*\), sing-box не отвечает \d+ '
                                         r'провер\w+ подряд - перезапустил sing-box$')


class Disabled(Base):
    def test_disabled_node_left_at_once_and_connections_closed(self):
        s = self.stand()
        s.conns = [{'id': 'user', 'chains': ['hy2-aa-direct', 'lane-01']},
                   {'id': 'mgmt', 'chains': ['hy2-aa-mgmt']}, {'id': 'other', 'chains': ['hy2-bb-direct']}]
        s.disable('aa')
        s.tick()
        self.assertEqual(s.now(), 'hy2-bb-direct')
        self.assertIn('узел aa выключен вручную', s.said('switch ->')[0])
        self.assertEqual(set(s.closed), {'user'})
        self.assertFalse(s.said('в штрафной'))
        s.dog.tg()
        self.assertTrue(all(s.now(g).startswith('hy2-bb-') for g in s.p if g.startswith(('lane-', 'tg-'))))
        self.assertEqual(s.now('pin-hy2-aa-direct'), 'foreign-best')
        self.assertEqual(s.now('pin-hy2-aa-warp'), 'hy2-bb-warp')

    def test_disabled_node_carries_traffic_when_nothing_else_alive(self):
        s = self.stand()
        s.disable('bb')
        s.tick()
        s.kill('aa')
        s.tick(20)
        s.conns = [{'id': 'user', 'chains': ['hy2-bb-direct', 'lane-01']}]
        s.tick(20)
        self.assertEqual(s.now(), 'hy2-bb-direct')
        self.assertEqual(s.closed, [])
        self.assertTrue(s.said('трафик идёт через выключенные вручную (bb)'))
        self.assertEqual(s.restarts, [])


class TcpReserve(Base):
    def test_udp_cut_moves_channel_to_tcp_and_back(self):
        s = self.stand()
        s.cut_udp('aa')
        s.tick(30)
        self.assertEqual(s.now('transport-aa-direct'), 'transport-tcp-aa-direct')
        self.assertEqual(s.now('hy2-aa-mgmt'), 'transport-tcp-aa-mgmt', 'mgmt идёт за direct')
        self.assertEqual(s.now(), 'hy2-aa-direct', 'экзит не меняем: узел жив по TCP')
        self.assertFalse(s.said('в штрафной'))
        self.assertEqual(s.events, [('UDP до экзита aa недоступен, работаю через TCP',
                                     'Hysteria2 до экзита не отвечает, каналы идут запасным VLESS + Reality '
                                     'по TCP, sing-box не перезапускаю')])
        s.cut_udp('aa', on=False)
        s.tick(fo.TCP_RETURN - 10)
        self.assertEqual(s.now('transport-aa-direct'), 'transport-tcp-aa-direct')
        s.tick(20)
        self.assertEqual(s.now('transport-aa-direct'), 'transport-hy2-aa-direct')
        self.assertEqual(s.now('hy2-aa-mgmt'), 'transport-hy2-aa-mgmt')
        self.assertEqual(s.events[-1], ('UDP до экзита aa снова работает',
                                        f'Hysteria2 жив {fo.TCP_RETURN:.0f} с подряд, каналы вернулись с TCP'))
        self.assertEqual(s.restarts, [])

    def test_flapping_udp_does_not_bounce_channel(self):
        s = self.stand()
        for _ in range(6):
            s.cut_udp('aa')
            s.tick(20)
            s.cut_udp('aa', on=False)
            s.tick(20)
        self.assertEqual(len(s.events), 1)
        self.assertEqual(s.now(), 'hy2-aa-direct')


class LanesPinsTg(Base):
    def test_lanes_leave_dead_node_and_spread(self):
        s = self.stand()
        s.tick(fo.REBALANCE_INTERVAL + fo.IDLE_DWELL)
        homes = [s.now(f'lane-{i:02d}') for i in range(1, 5)]
        self.assertEqual(sorted(homes), ['hy2-aa-direct'] * 2 + ['hy2-bb-direct'] * 2, 'спящие дорожки поровну')
        s.kill('aa')
        s.tick(20)
        self.assertEqual({s.now(f'lane-{i:02d}') for i in range(1, 5)}, {'hy2-bb-direct'})

    def test_lane_returns_from_warp_to_direct_after_quiet_time(self):
        s = self.stand(nodes=('aa',))
        s.down.add('hy2-aa-direct')
        s.tick(20)
        self.assertEqual(s.now('lane-01'), 'hy2-aa-warp')
        s.down.clear()
        s.tick(fo.WARP_RETURN - 30)
        self.assertEqual(s.now('lane-01'), 'hy2-aa-warp')
        s.tick(60)
        self.assertEqual(s.now('lane-01'), 'hy2-aa-direct')

    def test_balance_off_lanes_follow_main_exit(self):
        s = self.stand()
        fo.BALANCE = False
        s.kill('aa')
        s.tick(20)
        self.assertEqual({s.now(f'lane-{i:02d}') for i in range(1, 5)}, {'hy2-bb-direct'})

    def test_pin_falls_back_and_returns_after_penalty(self):
        s = self.stand()
        s.kill('bb')
        s.tick(20)
        self.assertEqual(s.now('pin-hy2-bb-direct'), 'foreign-best')
        self.assertEqual(s.now('pin-hy2-bb-warp'), 'hy2-aa-warp')
        s.kill('bb', on=False)
        s.conns = [{'id': 'via-reserve', 'chains': ['hy2-aa-direct', 'foreign-best', 'pin-hy2-bb-direct']}]
        s.tick(30)
        self.assertEqual(s.now('pin-hy2-bb-direct'), 'foreign-best', 'узел в штрафной')
        s.tick(40)
        self.assertEqual(s.now('pin-hy2-bb-direct'), 'hy2-bb-direct')
        self.assertEqual(s.now('pin-hy2-bb-warp'), 'hy2-bb-warp')
        self.assertIn('via-reserve', s.closed)

    def test_stuck_pin_exit_heals_by_restart_then_gives_up(self):
        s = self.stand()
        s.down.add('hy2-bb-warp')
        s.tick(fo.STUCK_AFTER * fo.INTERVAL + 30)
        self.assertEqual(s.events, [('Экзиты', 'Выход hy2-bb-warp завис при живом узле - перезапустил sing-box')])
        s.tick(3 * fo.RESTART_MIN_INTERVAL)
        self.assertEqual(len(s.restarts), 2)
        self.assertEqual(s.events[-1], ('Экзиты', 'Выход hy2-bb-warp не оживает после перезапуска sing-box (2×) - '
                                                  'лечить перестал, маршрут идёт через foreign-best'))
        s.tick(2 * fo.RESTART_MIN_INTERVAL)
        self.assertEqual(len(s.events), 3)

    def test_tg_moves_to_exit_where_bot_api_answers(self):
        s = self.stand()
        s.dog.tg()
        self.assertEqual(s.now('tg-best'), 'hy2-aa-direct')
        s.tg_ok = {'hy2-bb-direct', 'hy2-bb-warp'}
        for _ in range(fo.DEAD_AFTER):
            s.t += fo.TG_INTERVAL
            s.dog.tg()
        self.assertEqual(s.now('tg-best'), 'hy2-bb-direct')
        s.tg_ok = set()
        for _ in range(10):
            s.t += fo.TG_INTERVAL
            s.dog.tg()
        self.assertEqual(s.now('tg-best'), 'hy2-bb-direct', 'молчит везде - стоим')
        self.assertEqual(len(s.said('Bot API не отвечает ни через один экзит')), 1)


class Settings(unittest.TestCase):
    def test_bad_env_value_is_default(self):
        os.environ.update(X_OK='7', X_BAD='много')
        self.addCleanup(lambda: [os.environ.pop(k) for k in ('X_OK', 'X_BAD')])
        self.assertEqual([fo._env('X_OK', 3, int), fo._env('X_BAD', 3, int), fo._env('X_NONE', 2)], [7, 3, 2.0])

    def test_redeploy_keeps_documented_settings(self):
        old = 'DEAD_AFTER=5\nBALANCE=off\nTCP_RETURN_S=20\nINTERVAL=часто\nRESTART_AFTER_CHECKS=9\nCOOLDOWN_S=1\n'
        env = dict(line.split('=', 1) for line in failover_env.render(old, 'тайна', {'RESTART_AFTER_CHECKS': '0'})
                   .splitlines())
        self.assertEqual([env[k] for k in ('DEAD_AFTER', 'BALANCE', 'TCP_RETURN_S', 'INTERVAL', 'CLASH_SECRET')],
                         ['5', 'off', '20', '2', 'тайна'])
        self.assertEqual(env['RESTART_AFTER_CHECKS'], '0', 'только из config.yaml')
        self.assertNotIn('COOLDOWN_S', env)
        documented = Path(__file__).resolve().parents[2].joinpath('docs/failover.md').read_text()
        for key in {s[0] for s in failover_env.SETTINGS} - set(failover_env.FROM_CONFIG):
            self.assertIn(key, documented)


if __name__ == '__main__':
    unittest.main()
