import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from router_support import SB, generate


@unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
class IdnRouteTests(unittest.TestCase):
    def test_russian_idn_dns_matches_wire_format(self):
        cfg = json.loads(generate())
        rule = next(rule for rule in cfg['dns']['rules'] if 'domain_suffix' in rule)
        self.assertEqual(rule['server'], 'ru-dns')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dns-domains.json'
            path.write_text(json.dumps({'version': 2, 'rules': [
                {'domain_suffix': rule['domain_suffix']},
            ]}))
            for domain, expected in [('пример.рф', True), ('www.пример.рф', True),
                                     ('example.ru', True), ('example.su', True),
                                     ('example.com', False), ('example.xn--p1ai.com', False),
                                     ('example.notxn--p1ai', False)]:
                wire_domain = domain.encode('idna').decode('ascii')
                with self.subTest(domain=wire_domain):
                    result = subprocess.run([SB, 'rule-set', 'match', str(path), wire_domain],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual('match rules.' in result.stderr, expected, result.stderr)

    def test_russian_idn_matches_wire_format(self):
        cfg = json.loads(generate())
        rule = next(rule for rule in cfg['route']['rules'] if rule.get('type') == 'logical')
        domain_rule = rule['rules'][0]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'domains.json'
            path.write_text(json.dumps({'version': 2, 'rules': [domain_rule]}))
            for domain, expected in [('пример.рф', True), ('example.ru', True),
                                     ('example.su', True), ('example.com', False)]:
                wire_domain = domain.encode('idna').decode('ascii')
                with self.subTest(domain=wire_domain):
                    result = subprocess.run([SB, 'rule-set', 'match', str(path), wire_domain],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual('match rules.' in result.stderr, expected, result.stderr)


if __name__ == '__main__':
    unittest.main()
