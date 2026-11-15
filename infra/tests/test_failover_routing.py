"""Verify that generated foreign selectors cannot fall back to entry direct."""
import json
import os
from pathlib import Path
import unittest

from router_support import exit_cert, generate


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))


def router_config(tcp=False):
    env = {'EXIT_TAGS': 'aa bb', 'HY2_DIRECT_PORT': '443', 'HY2_WARP_PORT': '8443',
           'HY2_MGMT_PORT': '9443', 'WG_OUT_IFACE': 'lo', 'RU_CLASH_SECRET': 'test'}
    for n, tag in enumerate(('AA', 'BB'), start=2):
        env.update({f'DOMAIN_{tag}': '127.0.0.1', f'MGMT_IP_{tag}': f'192.0.2.{n}',
                    f'{tag}_HY2_CERT_PATH': exit_cert()})
        for key in ('DIRECT', 'WARP', 'MGMT', 'OBFS'):
            env[f'{tag}_{key}'] = 'test'
        if tcp:
            env.update({f'{tag}_REALITY_PUBKEY': 'pub', f'{tag}_REALITY_SHORT_ID': 'ab',
                        f'{tag}_REALITY_SNI': 'example.com', f'{tag}_REALITY_PORT': '8444'})
    return json.loads(generate(**env))


class TestFailoverRouting(unittest.TestCase):
    def test_all_foreign_paths_terminate_at_exit_tunnels(self):
        for tcp in (False, True):
            cfg = router_config(tcp)
            outbounds = {o['tag']: o for o in cfg['outbounds']}

            def leaves(tag, visited, muxed=False):
                self.assertNotIn(tag, visited, 'outbound cycle')
                out = outbounds[tag]
                if out['type'] in ('selector', 'urltest'):
                    # Смена выбора под h2mux рвала бы все потоки его сессий, а не только новые.
                    self.assertFalse(muxed, f'{tag} под h2mux')
                    self.assertFalse(out['interrupt_exist_connections'])
                    for child in out['outbounds']:
                        yield from leaves(child, visited | {tag}, muxed)
                elif out.get('detour'):
                    yield from leaves(out['detour'], visited | {tag}, muxed or 'multiplex' in out)
                else:
                    yield out

            self.assertEqual(cfg['route']['final'], 'foreign-best')
            for tag, out in outbounds.items():
                if out['type'] in ('selector', 'urltest'):
                    with self.subTest(tcp=tcp, tag=tag):
                        terminal = list(leaves(tag, set()))
                        self.assertTrue(terminal)
                        self.assertEqual({o['type'] for o in terminal}, {'hysteria2', 'vless'} if tcp else {'hysteria2'})
                        self.assertEqual(all(o['tag'].endswith('-mgmt') for o in terminal), tag.endswith('-mgmt'))


if __name__ == '__main__':
    unittest.main()
