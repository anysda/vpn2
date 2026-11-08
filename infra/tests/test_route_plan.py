import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from router_support import SB, apply, prepare, snapshot


def load(path):
    return json.loads(path.read_text())


@unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
class RoutePlanTests(unittest.TestCase):
    def test_plan_is_the_applied_config_with_set_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            script = prepare(directory)
            (directory / 'geoip-ru.json').write_text(json.dumps(
                {'version': 2, 'rules': [{'ip_cidr': ['198.51.100.0/24']}]}))
            (directory / 'manual.json').write_text(json.dumps([
                {'type': 'domain', 'value': '*.example.com', 'outbound': 'hy2-bb-warp'}]))
            result = apply(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            plan, cfg = load(directory / 'plan.json'), load(directory / 'config.json')
            self.assertEqual(plan['rules'], cfg['route']['rules'])
            self.assertEqual(plan['final'], cfg['route']['final'])
            self.assertEqual(set(plan['rule_set']), {s['tag'] for s in cfg['route']['rule_set']})
            self.assertEqual(plan['rule_set']['geoip-ru'], [{'ip_cidr': ['198.51.100.0/24']}])
            self.assertEqual(plan['rule_set']['manual-pin-hy2-bb-warp'], [{'domain_suffix': ['example.com']}])

    def test_route_edit_updates_plan_without_config_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            script = prepare(directory)
            self.assertEqual(apply(script).returncode, 0)
            config = (directory / 'config.json').read_bytes()
            (directory / 'manual.json').write_text(json.dumps([
                {'type': 'ip_cidr', 'value': '203.0.113.0/24', 'outbound': 'direct-ru'}]))
            result = apply(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((directory / 'config.json').read_bytes(), config)
            self.assertEqual(load(directory / 'plan.json')['rule_set']['manual-direct-ru'],
                             [{'ip_cidr': ['203.0.113.0/24']}])

    def test_binary_sets_are_decompiled(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            script = prepare(directory)
            source = directory / 'geoip-ru.json'
            source.write_text(json.dumps({'version': 2, 'rules': [{'ip_cidr': ['198.51.100.0/24']}]}))
            subprocess.run([SB, 'rule-set', 'compile', '-o', str(directory / 'geoip-ru.srs'), str(source)], check=True)
            base = load(directory / 'base.json')
            for item in base['route']['rule_set']:
                if item['tag'] == 'geoip-ru':
                    item.update(format='binary', path=str(directory / 'geoip-ru.srs'))
            (directory / 'base.json').write_text(json.dumps(base))
            result = apply(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            rules = load(directory / 'plan.json')['rule_set']['geoip-ru']
            self.assertEqual([r['ip_cidr'] for r in rules], ['198.51.100.0/24'])

    def test_broken_input_keeps_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            script = prepare(directory)
            self.assertEqual(apply(script).returncode, 0)
            before = snapshot(directory)
            self.assertIn('plan.json', before)
            (directory / 'manual.json').write_text('{')
            self.assertNotEqual(apply(script).returncode, 0)
            self.assertEqual(snapshot(directory), before)


if __name__ == '__main__':
    unittest.main()
