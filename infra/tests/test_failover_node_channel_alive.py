"""Мёртвый канал узла при живом другом канале того же узла - не смерть узла.

Обрыв UDP до экзита: hy2 каналов direct и warp мёртв, оба уходят на TCP. Если
TCP-проба direct промахнулась на весь тик (все до-проверки), direct остаётся на
мёртвом hy2 ещё тик, а warp того же узла уже везёт трафик по TCP. Узел жив:
штрафная и уход ручного маршрута direct в foreign-best тут без причины.
Правило одно для direct и warp: штрафуем узел, только когда у него не отвечает
ни один выход пользовательского трафика (mgmt такого трафика не несёт).

clash-api - модель из test_tcp_fallback по настоящему конфигу генератора;
выход мёртв, если мёртв любой тег на пути к листу (bad), проба тега напрямую
промахивается заданное число раз (miss).
"""
import socket
import tempfile
import unittest
import urllib.error
import urllib.parse

import test_tcp_fallback as tf
import test_failover_restart as fr


class ChannelClash(tf.Clash):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.bad = set()
        self.miss = {}

    def path(self, tag):
        names = [tag]
        o = self.obs[tag]
        while o['type'] in ('selector', 'urltest') or o.get('detour'):
            tag = self.now[tag] if o['type'] in ('selector', 'urltest') else o['detour']
            names.append(tag)
            o = self.obs[tag]
        return names

    def alive(self, tag):
        return not self.bad & set(self.path(tag)) and super().alive(tag)

    def req(self, method, path, body=None, **kw):
        name = urllib.parse.unquote(path.split('?')[0]).split('/')[2] \
            if path.startswith('/proxies/') else ''
        if method == 'GET' and path.split('?')[0].endswith('/delay') and self.miss.get(name):
            self.miss[name] -= 1
            raise urllib.error.URLError(socket.timeout('timed out'))
        return super().req(method, path, body, **kw)


class TestNodeChannelAlive(unittest.TestCase):
    def start(self, tmp):
        w = fr.watchdog(tmp)
        clash = ChannelClash(tf.router_config(('aa', 'bb')))
        clock = fr.Clock(w.INTERVAL)
        run = fr.Run(w, clash, clock)
        run.go(3)
        self.assertEqual(clash.now['pin-hy2-aa-direct'], 'hy2-aa-direct')
        return w, clash, clock, run

    def on_tcp(self, clash, kind):
        return tf.is_reality(clash.leaf(f'hy2-aa-{kind}'))

    def log(self, run):
        return '\n' + run.out.getvalue()[-2500:]

    def penalized(self, w, clock):
        return w._penalty.get('aa', 0.0) > clock.mono

    def watch(self, w, clash, clock, run, ticks):
        """Тиков до direct на TCP; штраф aa и pin direct не на своём выходе - за все тики."""
        took, pen, pin = None, False, set()
        for k in range(1, ticks + 1):
            run.go(1)
            pen = pen or self.penalized(w, clock)
            if clash.now['pin-hy2-aa-direct'] != 'hy2-aa-direct':
                pin.add(clash.now['pin-hy2-aa-direct'])
            if took is None and self.on_tcp(clash, 'direct'):
                took = k
        return took, pen, pin

    def udp_cut_direct_tcp_miss(self, whole_tick):
        """Обрыв UDP до aa, TCP-проба direct промахивается один раз или весь тик
        (все DEAD_AFTER проб с до-проверками, как на стенде)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        w, clash, clock, run = self.start(tmp.name)
        clash.udp.discard('aa')
        clash.miss['transport-tcp-aa-direct'] = w.DEAD_AFTER if whole_tick else 1
        return w, clash, clock, run

    def test_dead_direct_hy2_with_warp_on_tcp_does_not_penalize_node(self):
        """(а) hy2 direct мёртв, TCP direct промахнулся, warp на TCP жив."""
        for whole_tick in (False, True):
            with self.subTest(whole_tick=whole_tick):
                w, clash, clock, run = self.udp_cut_direct_tcp_miss(whole_tick)
                _, pen, pin = self.watch(w, clash, clock, run, 10)
                self.assertTrue(self.on_tcp(clash, 'warp'), 'warp не ушёл на TCP' + self.log(run))
                self.assertFalse(pen, f'TCP direct промахнулся (весь тик: {whole_tick}), warp узла aa жив на TCP, '
                                      'а узел в штрафной' + self.log(run))
                self.assertEqual(pin, set(), 'pin-hy2-aa-direct ушёл с узла при живом warp'
                                 + self.log(run))
                self.assertNotIn('в штрафной', run.out.getvalue(), self.log(run))

    def test_dead_direct_hy2_goes_to_tcp_after_recheck(self):
        """(б) direct уходит на TCP за DEAD_AFTER тиков, а не остаётся на мёртвом hy2."""
        for whole_tick in (False, True):
            with self.subTest(whole_tick=whole_tick):
                w, clash, clock, run = self.udp_cut_direct_tcp_miss(whole_tick)
                took, _, _ = self.watch(w, clash, clock, run, 10)
                self.assertIsNotNone(took, 'direct не ушёл на TCP' + self.log(run))
                self.assertLessEqual(took, w.DEAD_AFTER,
                                     f'direct на TCP через {took} тиков, ждём не больше '
                                     f'{w.DEAD_AFTER}' + self.log(run))
                self.assertEqual(run.restarts, [])

    def test_dead_warp_with_direct_on_tcp_does_not_penalize_node(self):
        """(в) зеркало: warp мёртв на обоих транспортах, direct на TCP жив."""
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, clock, run = self.start(tmp)
            clash.now[w.GROUP] = 'hy2-aa-warp'
            clash.udp.discard('aa')
            clash.bad.add('transport-tcp-aa-warp')
            _, pen, pin = self.watch(w, clash, clock, run, 10)
            self.assertTrue(self.on_tcp(clash, 'direct'), 'direct не ушёл на TCP' + self.log(run))
            self.assertFalse(pen, 'warp мёртв, direct узла aa жив на TCP, а узел в штрафной'
                             + self.log(run))
            self.assertEqual(pin, set(), self.log(run))
            self.assertNotEqual(clash.now[w.GROUP], 'hy2-aa-warp',
                                'основной выбор остался на мёртвом warp' + self.log(run))

    def test_all_user_channels_dead_penalizes_node(self):
        """(г) мертвы hy2 и TCP у direct и warp - штраф, как раньше."""
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, clock, run = self.start(tmp)
            clash.udp.discard('aa')
            clash.tcp.discard('aa')
            _, pen, _ = self.watch(w, clash, clock, run, 4)
            self.assertTrue(pen, 'все выходы узла aa мертвы, а штрафа нет' + self.log(run))
            self.assertTrue(clash.now[w.GROUP].startswith('hy2-bb-'), self.log(run))

    def test_only_mgmt_alive_penalizes_node(self):
        """(д) жив только mgmt - пользовательский трафик узел не везёт, штраф."""
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, clock, run = self.start(tmp)
            clash.bad.update(f'transport-{t}-aa-{k}' for t in ('hy2', 'tcp')
                             for k in ('direct', 'warp'))
            self.assertTrue(clash.alive('hy2-aa-mgmt'))
            _, pen, _ = self.watch(w, clash, clock, run, 4)
            self.assertTrue(pen, 'у aa жив только mgmt, а штрафа нет' + self.log(run))
            self.assertTrue(clash.now[w.GROUP].startswith('hy2-bb-'), self.log(run))


if __name__ == '__main__':
    unittest.main()
