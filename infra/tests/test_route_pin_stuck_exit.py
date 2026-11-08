"""VPN2-649: правило «домен → выход» есть, а трафик идёт через другой экзит.

На хабе маршрут панели «claude.ai → US Напрямую» доходит до sing-box целиком:
manual-routes.json, набор manual-pin-hy2-us-direct, правило в config.json выше
дорожек, соединения FUJI матчатся им. Теряется выход: pin-hy2-us-direct стоит
на foreign-best (FR) с тех пор, как hy2-us-direct замолчал. Узел us при этом
жив (транспорт hysteria2 и warp отвечают), а сам hy2-us-direct в процессе
sing-box хаба завис и без перезапуска не оживает. Сторож перезапускает
sing-box, только когда мертвы все выходы, поэтому ручной маршрут сутками
остаётся на запасном экзите.

Сторож гоняется настоящим main() (харнесс test_failover_restart); края
маршрута — настоящим скриптом применения и sing-box (router_support).
"""
import json
from pathlib import Path
import tempfile
import unittest

from router_support import SB, apply, prepare
from test_failover_restart import Clash, Clock, Run, watchdog


PIN = 'pin-hy2-aa-direct'
STUCK = 'hy2-aa-direct'


class StuckClash(Clash):
    """Узлы aa и bb живы, но hy2-aa-direct в процессе sing-box завис:
    delay-проба через него не проходит, пока sing-box не перезапущен."""

    def __init__(self):
        super().__init__(['aa', 'bb'])
        self.alive = set(self.tags) - {STUCK}
        self.now[PIN] = STUCK


class StuckRun(Run):
    def restart(self, *args, **kwargs):
        super().restart(*args, **kwargs)
        self.clash.alive.add(STUCK)


class TestPinOnStuckExit(unittest.TestCase):
    # 1
    def test_manual_route_returns_to_its_exit_when_node_is_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = watchdog(tmp, INTERVAL='2')
            clash = StuckClash()
            run = StuckRun(w, clash, Clock(w.INTERVAL)).go(int(1800 / w.INTERVAL))
            self.assertEqual(
                clash.now[PIN], STUCK,
                f'полчаса узел aa жив (warp отвечает), завис только {STUCK}: ручной маршрут '
                f'на нём должен вернуться на свой выход, а {PIN} = {clash.now[PIN]}, '
                f'перезапусков sing-box {len(run.restarts)}:\n' + run.out.getvalue()[-600:])


def rule_index(cfg, pred):
    return next((i for i, r in enumerate(cfg['route']['rules']) if pred(r)), None)


@unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.14.4')
class TestManualRouteEdges(unittest.TestCase):
    def run_apply(self, rows):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        script = prepare(directory)
        (directory / 'manual.json').write_text(json.dumps(rows))
        result = apply(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        cfg = json.loads((directory / 'config.json').read_text())
        return directory, cfg, result

    def rules_of(self, directory, out):
        return json.loads((directory / 'rules' / f'{out}.json').read_text())['rules']

    # 2
    def test_domain_to_existing_exit_goes_to_its_set(self):
        directory, cfg, _ = self.run_apply(
            [{'type': 'domain', 'value': 'claude.ai', 'outbound': STUCK}])
        self.assertIn({'domain_suffix': ['claude.ai']}, self.rules_of(directory, PIN))
        self.assertIsNotNone(rule_index(cfg, lambda r: r.get('rule_set') == [f'manual-{PIN}']
                                        and r.get('outbound') == PIN))

    # 3
    def test_wildcard_domain_is_suffix(self):
        directory, _, _ = self.run_apply(
            [{'type': 'domain', 'value': '*.steampowered.com', 'outbound': STUCK}])
        self.assertIn({'domain_suffix': ['steampowered.com']}, self.rules_of(directory, PIN))

    # 4
    def test_exit_with_pin_routes_via_pin(self):
        directory, _, _ = self.run_apply(
            [{'type': 'domain', 'value': 'claude.ai', 'outbound': STUCK}])
        self.assertFalse((directory / 'rules' / f'{STUCK}.json').exists(),
                         'маршрут на выход с pin- лёг мимо pin')

    # 5
    def test_missing_exit_is_reported_and_others_kept(self):
        directory, _, result = self.run_apply(
            [{'type': 'domain', 'value': 'gone.example', 'outbound': 'hy2-zz-direct'},
             {'type': 'domain', 'value': 'claude.ai', 'outbound': STUCK}])
        self.assertIn('hy2-zz-direct', result.stderr, 'строка на несуществующий выход пропала молча')
        self.assertIn({'domain_suffix': ['claude.ai']}, self.rules_of(directory, PIN))

    # 6
    def test_domain_rule_is_above_lane_rules(self):
        _, cfg, _ = self.run_apply(
            [{'type': 'domain', 'value': 'claude.ai', 'outbound': STUCK}])
        manual = rule_index(cfg, lambda r: r.get('rule_set') == [f'manual-{PIN}'])
        lane = rule_index(cfg, lambda r: str(r.get('outbound', '')).startswith('lane-'))
        self.assertIsNotNone(lane, 'в конфиге нет дорожек')
        self.assertLess(manual, lane, 'правило дорожки стоит выше доменного')


if __name__ == '__main__':
    unittest.main()
