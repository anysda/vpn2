"""Offline watchdog regressions; VPN2_AUDIT_ROOT selects the source revision."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
A = 'hy2-aa-direct'
B = 'hy2-bb-direct'


def watchdog():
    spec = importlib.util.spec_from_file_location('watchdog', ROOT / 'infra/lib/failover-watchdog.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {}, clear=True):
        spec.loader.exec_module(module)
    return module


class TestFailoverAudit(unittest.TestCase):
    def test_confirmed_recovery_reaches_lanes_and_pins(self):
        w = watchdog()
        group = {'type': 'Selector', 'all': [A, B], 'now': A}
        pin = 'pin-' + A
        proxies = {pin: {'type': 'Selector', 'now': A}}

        def recover(tag, out, errs):
            self.assertEqual(tag, A)
            self.assertEqual(errs, {})
            out[tag] = 75

        with patch.object(w, '_req', return_value=group) as req, \
                patch.object(w, '_read_disabled', return_value=set()), \
                patch.object(w, '_probe_all', return_value=({A: None, B: 100}, {})), \
                patch.object(w, '_probe', side_effect=recover), \
                patch.object(w.time, 'sleep'):
            for _ in range(w.DEAD_AFTER + 1):
                w.tick()
                w.pins_tick(proxies)
                alive = w._effective_alive(w._tick_alive, [A, B])
            self.assertIn(A, alive, 'successful confirmation must keep the lane exit alive')
            self.assertEqual(w._pin_miss[A], 0)
            self.assertEqual(w._penalty, {})
            self.assertTrue(all(c.args[0] == 'GET' for c in req.call_args_list))

    def test_confirmed_failure_switches_and_penalizes(self):
        w = watchdog()
        group = {'type': 'Selector', 'all': [A, B], 'now': A}
        with patch.object(w, '_req', return_value=group) as req, \
                patch.object(w, '_read_disabled', return_value=set()), \
                patch.object(w, '_probe_all', return_value=({A: None, B: 100}, {})), \
                patch.object(w, '_probe'), patch.object(w.time, 'sleep'):
            w.tick()
        self.assertIn('aa', w._penalty)
        req.assert_called_with('PUT', '/proxies/foreign-best', {'name': B})

    def test_total_outage_preserves_foreign_selection(self):
        w = watchdog()
        group = {'type': 'Selector', 'all': [A, B], 'now': A}
        with patch.object(w, '_req', return_value=group) as req, \
                patch.object(w, '_read_disabled', return_value=set()), \
                patch.object(w, '_probe_all', return_value=({}, {})):
            w.tick()
        self.assertEqual(w._tick_alive, {})
        self.assertTrue(all(c.args[0] == 'GET' for c in req.call_args_list))

    def test_connection_api_failure_preserves_healthy_lanes(self):
        w = watchdog()
        w._tick_alive = {A: 100, B: 100}
        proxies = {f'lane-{n:02}': {'type': 'Selector', 'now': A, 'all': [A, B]}
                   for n in range(4)}
        with patch.object(w, '_req', return_value={'proxies': proxies}) as req, \
                patch.object(w.time, 'monotonic', return_value=10000), \
                patch.object(w, '_last_rebalance', 0):
            w.lanes_tick(None)
            self.assertTrue(all(c.args[0] == 'GET' for c in req.call_args_list),
                            'missing connection snapshot must not mean idle lanes')
            w.lanes_tick([])
        self.assertTrue(any(c.args[0] == 'PUT' for c in req.call_args_list),
                        'a successful empty snapshot should still allow idle balancing')

    def test_connection_api_failure_still_evacuates_dead_exit(self):
        w = watchdog()
        w._tick_alive = {B: 100}
        proxies = {'lane-00': {'type': 'Selector', 'now': A, 'all': [A, B]}}
        with patch.object(w, '_req', return_value={'proxies': proxies}) as req:
            w.lanes_tick(None)
        req.assert_called_with('PUT', '/proxies/lane-00', {'name': B})

    def test_failed_persistence_is_retried_without_another_move(self):
        w = watchdog()
        with tempfile.TemporaryDirectory() as tmp:
            w.LANE_STATE_FILE = str(Path(tmp) / 'lanes.json')
            w._lane_moved = {'lane-00': 100.0}
            with patch.object(w.os, 'replace', side_effect=OSError('disk temporarily unavailable')):
                w._persist_lanes()
            w._persist_lanes()
            self.assertTrue(Path(w.LANE_STATE_FILE).is_file(), 'the next tick must retry')
            self.assertEqual(set(json.loads(Path(w.LANE_STATE_FILE).read_text())), {'lane-00'})
            self.assertEqual(w._lane_saved, w._lane_moved)
            with patch.object(w.os, 'replace') as replace:
                w._persist_lanes()
                replace.assert_not_called()


if __name__ == '__main__':
    unittest.main()
