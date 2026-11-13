"""Счётчики IKEv2: разбор `swanctl --list-sas` в сессии ikev2-status.json."""
import types
import unittest

from infra_support import ROOT

SAS = '''anysda-ikev2: #7, ESTABLISHED, IKEv2, cbd5867eca6763b0_i 0cf6cdcbdda975d6_r*
  remote '192.0.2.10' @ 198.51.100.7[1445] EAP: 'phone' [10.68.68.1]
  net: #7, reqid 1, INSTALLED, TUNNEL, ESP:AES_GCM_16-256
    in  c6cbc42a (-|0x0000002a),  50650 bytes,   540 packets,    11s ago
    out ceebcf43 (-|0x0000002a), 1023795 bytes,  1100 packets,    11s ago
anysda-dev-3: #9, ESTABLISHED, IKEv2, aa_i bb_r*
  remote 'laptop' @ 198.51.100.8[4500] [10.68.68.3]
    in  c6cbc42b,  10 bytes,   5 packets,    1s ago
    out ceebcf44,  20 bytes,  5 packets,    1s ago
anysda-ikev2: #10, CONNECTING, IKEv2, cc_i dd_r
'''


class Ikev2SyncTests(unittest.TestCase):
    def test_sessions(self):
        script = (ROOT / 'infra/ansible/roles/ikev2/files/anysda-ikev2-sync.py').read_text()
        sync = types.ModuleType('ikev2_sync')
        exec(compile(script, 'ikev2_sync', 'exec'), sync.__dict__)
        self.assertEqual(sync.sessions(SAS), [
            {'uniqueid': '7', 'username': 'phone', 'rx': 1023795, 'tx': 50650},
            {'uniqueid': '9', 'username': 'laptop', 'rx': 20, 'tx': 10}])


if __name__ == '__main__':
    unittest.main()
