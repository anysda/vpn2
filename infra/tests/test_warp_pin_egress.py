"""Ручной маршрут на «-warp» выходит через WARP, а не direct-IP экзита (VPN2-650).

Прод 02-11: сторож хаба на минуту признаёт hy2-<узел>-warp мёртвым и переводит
pin-hy2-<узел>-warp на foreign-best, а там direct-ноги: «-warp» выходит с IP
дата-центра. Экзит при этом считает WARP живым. Запасной путь warp-маршрута
должен остаться на WARP либо держать маршрут на мёртвой ноге видно, но не
уводить его в direct молча.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from router_support import exit_cert
import watchdog_src


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
AA_DIRECT, AA_WARP = 'hy2-aa-direct', 'hy2-aa-warp'
BB_DIRECT, BB_WARP = 'hy2-bb-direct', 'hy2-bb-warp'


def router_config():
    spec = importlib.util.spec_from_file_location('router', ROOT / 'infra/vpn2/src/vpn2/router/config.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = {'EXIT_TAGS': 'aa bb', 'HY2_DIRECT_PORT': '443', 'HY2_WARP_PORT': '8443',
           'HY2_MGMT_PORT': '9443', 'WG_OUT_IFACE': 'lo', 'RU_CLASH_SECRET': 'test'}
    for n, tag in enumerate(('AA', 'BB'), start=2):
        env.update({f'DOMAIN_{tag}': '127.0.0.1', f'MGMT_IP_{tag}': f'192.0.2.{n}',
                    f'{tag}_HY2_CERT_PATH': exit_cert()})
        for key in ('DIRECT', 'WARP', 'MGMT', 'OBFS'):
            env[f'{tag}_{key}'] = 'test'
    output = io.StringIO()
    with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output):
        module.main()
    return json.loads(output.getvalue())


def watchdog():
    spec = importlib.util.spec_from_file_location('watchdog', watchdog_src.path('failover'))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {'DEAD_AFTER': '3'}, clear=True):
        spec.loader.exec_module(module)
    return module


def exit_legs(outbounds, tag, seen=()):
    """Ноги экзита (hy2-<узел>-direct|warp), до которых доходит группа."""
    out = outbounds[tag]
    if out['type'] not in ('selector', 'urltest'):
        return {tag}
    if tag in seen:
        return set()
    return set().union(*(exit_legs(outbounds, child, seen + (tag,)) for child in out['outbounds']))


class WarpPinRouterConfigTests(unittest.TestCase):
    def test_warp_pin_never_reaches_direct_leg(self):
        outbounds = {o['tag']: o for o in router_config()['outbounds']}
        for leg in (AA_WARP, BB_WARP):
            with self.subTest(pin=f'pin-{leg}'):
                legs = exit_legs(outbounds, f'pin-{leg}')
                direct = sorted(t for t in legs if not t.endswith('-warp'))
                self.assertEqual(direct, [], f'pin-{leg} доходит до direct-ног: «-warp» выйдет с IP экзита')

    def test_direct_pin_keeps_foreign_fallback(self):
        outbounds = {o['tag']: o for o in router_config()['outbounds']}
        self.assertEqual(outbounds[f'pin-{AA_DIRECT}']['outbounds'][0], AA_DIRECT)
        self.assertIn(BB_DIRECT, exit_legs(outbounds, f'pin-{AA_DIRECT}'))


class WarpPinWatchdogTests(unittest.TestCase):
    def dead_leg_puts(self, pin, alive):
        w = watchdog()
        proxies = {pin: {'type': 'Selector', 'now': pin[len('pin-'):]},
                   'foreign-best': {'type': 'Selector', 'now': BB_DIRECT}}
        with patch.object(w, '_tick_alive', alive), patch.object(w, '_req', return_value={}) as req, \
                contextlib.redirect_stdout(io.StringIO()):
            for _ in range(w.DEAD_AFTER + 1):
                w.pins_tick(proxies)
        return [c.args[2]['name'] for c in req.call_args_list
                if c.args[:2] == ('PUT', f'/proxies/{pin}')]

    def test_dead_warp_leg_does_not_move_pin_to_direct(self):
        # Сторож экзита WARP видит живым, хаб - три промаха подряд у aa-warp.
        targets = self.dead_leg_puts(f'pin-{AA_WARP}', {AA_DIRECT: 120, BB_DIRECT: 120, BB_WARP: 160})
        self.assertNotIn('foreign-best', targets,
                         'pin-hy2-aa-warp ушёл на foreign-best (сейчас hy2-bb-direct): «-warp» с direct-IP')
        self.assertTrue(all(t.endswith('-warp') or t == 'warp-best' for t in targets), targets)

    def test_dead_direct_leg_still_falls_back(self):
        targets = self.dead_leg_puts(f'pin-{AA_DIRECT}', {BB_DIRECT: 120, BB_WARP: 160})
        self.assertEqual(targets[-1:], ['foreign-best'])


if __name__ == '__main__':
    unittest.main()
