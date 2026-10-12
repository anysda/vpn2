"""Verify that generated foreign selectors cannot fall back to entry direct."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))


def router_config():
    spec = importlib.util.spec_from_file_location('router', ROOT / 'infra/lib/gen-router-config.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = {'EXIT_TAGS': 'aa bb', 'HY2_DIRECT_PORT': '443', 'HY2_WARP_PORT': '8443',
           'HY2_MGMT_PORT': '9443', 'WG_OUT_IFACE': 'lo', 'RU_CLASH_SECRET': 'test'}
    for n, tag in enumerate(('AA', 'BB'), start=2):
        env.update({f'DOMAIN_{tag}': '127.0.0.1', f'MGMT_IP_{tag}': f'192.0.2.{n}'})
        for key in ('DIRECT', 'WARP', 'MGMT', 'OBFS'):
            env[f'{tag}_{key}'] = 'test'
    output = io.StringIO()
    with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output):
        module.main()
    return json.loads(output.getvalue())


class TestFailoverRouting(unittest.TestCase):
    def test_all_foreign_paths_terminate_at_exit_tunnels(self):
        cfg = router_config()
        outbounds = {o['tag']: o for o in cfg['outbounds']}

        def leaves(tag, visited):
            self.assertNotIn(tag, visited, 'outbound cycle')
            out = outbounds[tag]
            if out['type'] in ('selector', 'urltest'):
                self.assertFalse(out['interrupt_exist_connections'])
                for child in out['outbounds']:
                    yield from leaves(child, visited | {tag})
            else:
                yield out

        self.assertEqual(cfg['route']['final'], 'foreign-best')
        for tag, out in outbounds.items():
            if out['type'] in ('selector', 'urltest'):
                with self.subTest(tag=tag):
                    terminal = list(leaves(tag, set()))
                    self.assertTrue(terminal)
                    self.assertTrue(all(o['type'] == 'hysteria2' for o in terminal))
                    self.assertTrue(all(not o['tag'].endswith('-mgmt') for o in terminal))


if __name__ == '__main__':
    unittest.main()
