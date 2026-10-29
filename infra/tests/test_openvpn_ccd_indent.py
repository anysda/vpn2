"""An indented "disable" in a ccd is a deny: openvpn skips leading whitespace on every line."""
import unittest
from unittest.mock import patch

import test_control

INDENTS = ('', '  ', '\t', '\x0b')


class OpenvpnCcdIndentTests(unittest.TestCase):
    setUpClass = classmethod(test_control.ControlTests.setUpClass.__func__)
    setUp = test_control.ControlTests.setUp
    request = test_control.ControlTests.request
    file = test_control.ControlTests.file
    openvpn_ca = test_control.ControlTests.openvpn_ca
    later = test_control.ControlTests.later
    EXPIRES = test_control.ControlTests.EXPIRES
    LATER = test_control.ControlTests.LATER

    def test_indented_disable_is_not_allowed(self):
        command, _failures = self.openvpn_ca()
        ccd = self.root / 'etc/openvpn/server/ccd/dev_7'
        with patch.object(self.backend, 'command', side_effect=command):
            state = self.request('openvpn.certificate', {'device_id': 7, 'serial': '0A'})
            self.assertTrue(state['result']['allowed'], state)
            for indent in INDENTS:
                with self.subTest(indent=ascii(indent)):
                    ccd.write_text(f'ifconfig-push 10.67.67.2 255.255.255.0\n{indent}disable\n')
                    state = self.request('openvpn.certificate', {'device_id': 7, 'serial': '0A'})
                    self.assertFalse(state['result']['allowed'], state)

    def test_expired_device_with_indented_disable_is_not_denied_again(self):
        self.file('/etc/openvpn/server/pki/index.txt', 'V\t300101000000Z\t\t0A\tunknown\t/CN=dev_7\n')
        ccd = self.file('/etc/openvpn/server/ccd/dev_7', '')
        params = {'version': 1, 'device_id': 7, 'action': 'allow', 'ip': '10.67.67.2',
                  'expires_at': self.EXPIRES}
        with patch.object(self.backend, 'command', return_value=''), \
                patch.object(self.backend, 'management', return_value=['END']):
            self.assertTrue(self.request('openvpn.apply', params)['ok'])
            for indent in INDENTS:
                with self.subTest(indent=ascii(indent)), self.later():
                    body = f'ifconfig-push 10.67.67.2 255.255.255.0\n{indent}disable\n'
                    ccd.write_text(body)
                    result = self.request('expiry.run', {})['result']
                    self.assertEqual(result['latest'], [])
                    self.assertEqual(ccd.read_text(), body)


if __name__ == '__main__':
    unittest.main()
