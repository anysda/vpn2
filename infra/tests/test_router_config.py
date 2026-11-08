import json
from pathlib import Path
import subprocess
from string import Template
import tempfile
import unittest

from router_support import ROOT, SB, apply, generate, prepare


class RouterConfigTests(unittest.TestCase):
    def test_generation_is_deterministic_and_exit_only_selectors(self):
        body = generate()
        self.assertEqual(body, generate())
        cfg = json.loads(body)
        self.assertEqual(cfg['route']['final'], 'foreign-best')
        for outbound in cfg['outbounds']:
            if outbound['type'] in ('selector', 'urltest'):
                self.assertNotIn('direct-ru', outbound['outbounds'])
                self.assertTrue(all(tag.startswith('hy2-') or tag == 'foreign-best'
                                    for tag in outbound['outbounds']))

    def test_empty_exit_list_fails(self):
        for tags in ('', '   '):
            with self.subTest(tags=tags), self.assertRaises(subprocess.CalledProcessError):
                generate(EXIT_TAGS=tags)

    def test_youtube_precedes_geoip_and_lanes(self):
        cfg = json.loads(generate(YT_ROUTE='zapret', YT_QUIC='block'))
        rules = cfg['route']['rules']
        # Российские правила обёрнуты в logical с исключением сетей Telegram:
        # условие лежит первым вложенным правилом.
        conds = [r['rules'][0] if r.get('type') == 'logical' else r for r in rules]
        youtube = next(i for i, r in enumerate(rules) if r.get('outbound') == 'youtube-auto')
        geoip = next(i for i, c in enumerate(conds) if c.get('rule_set') == ['geoip-ru'])
        lane = next(i for i, r in enumerate(rules) if r.get('outbound', '').startswith('lane-'))
        self.assertLess(youtube, geoip)
        self.assertLess(geoip, lane)
        self.assertTrue(any(r.get('action') == 'reject' and c.get('network') == 'udp'
                            and c.get('port') == [443]
                            for r, c in zip(rules[:youtube], conds[:youtube])))

    @unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
    def test_all_youtube_modes_pass_sing_box_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            prepare(directory)
            sets = json.loads((directory / 'base.json').read_text())['route']['rule_set']
            for mode in ('off', 'direct', 'zapret'):
                for quic in ('block', 'allow'):
                    with self.subTest(mode=mode, quic=quic):
                        cfg = json.loads(generate(YT_ROUTE=mode, YT_QUIC=quic))
                        cfg['route']['rule_set'] = sets
                        path = directory / 'check.json'
                        path.write_text(json.dumps(cfg))
                        result = subprocess.run([SB, 'check', '-c', str(path)],
                                                capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
    def test_manual_domain_and_ip_priority_in_real_rule_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            script = prepare(directory)
            (directory / 'manual.json').write_text(json.dumps([
                {'type': 'domain', 'value': 'example.com', 'outbound': 'hy2-bb-direct'},
                {'type': 'ip_cidr', 'value': '203.0.113.0/24', 'outbound': 'hy2-bb-direct'},
                {'type': 'domain', 'value': '*.example.com', 'outbound': 'direct-ru'},
                {'type': 'ip_cidr', 'value': '203.0.113.0/25', 'outbound': 'direct-ru'},
            ]))
            result = apply(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            for target in ('www.example.com', '203.0.113.10'):
                for outbound, matches in [('direct-ru', False), ('pin-hy2-bb-direct', True)]:
                    result = subprocess.run([SB, 'rule-set', 'match',
                                             str(directory / 'rules' / (outbound + '.json')), target],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual('match rules.' in result.stderr, matches, result.stderr)

    @unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
    def test_exit_template_passes_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                            'ec_paramgen_curve:P-256', '-nodes', '-days', '1',
                            '-subj', '/CN=exit.example.test', '-keyout', str(directory / 'tls.key'),
                            '-out', str(directory / 'tls.crt')], check=True, capture_output=True)
            env = {
                'HY2_DIRECT_PORT': '1443', 'HY2_WARP_PORT': '2443', 'HY2_MGMT_PORT': '3443',
                'WGCF_LOCAL_IPV4': '192.0.2.10', 'WGCF_PEER_ENDPOINT_HOST': '127.0.0.1',
                'WGCF_PEER_ENDPOINT_PORT': '2408', 'WGCF_RESERVED_JSON': '[0,0,0]', 'HY2_BANDWIDTH_FIELDS': '',
                'WGCF_PRIVKEY': 'wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0=',
                'WGCF_PEER_PUBKEY': 'wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0=',
            }
            for key in ('HY2_PWD_DIRECT', 'HY2_PWD_WARP', 'HY2_PWD_MGMT', 'HY2_OBFS_PWD', 'CLASH_SECRET'):
                env[key] = 'test-password-0123456789'
            template = (ROOT / 'infra/configs/sing-box-server.json.tpl').read_text()
            path = directory / 'exit.json'
            path.write_text(Template(template).substitute(env).replace('/etc/sing-box', str(directory)))
            result = subprocess.run([SB, 'check', '-c', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
