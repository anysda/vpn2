"""Switching the OpenVPN management user does not restart OpenVPN again on the next deploy."""
import tempfile
import unittest
from pathlib import Path

from test_deploy import bash, source


def between(text, start, end):
    return text[text.index(start):text.index(end, text.index(start))]


class OpenvpnRestartTests(unittest.TestCase):
    def test_management_user_switch_keeps_fingerprint_for_next_deploy(self):
        stage = between(source('infra/scripts/29-openvpn.sh'),
                        'echo "[$HOST_TAG] [5/6] openvpn-server@server"', 'svc_settle 2')
        switch = between(source('infra/scripts/30-frontend.sh'),
                         'if [[ -f /etc/openvpn/server/server.conf ]]', 'touch /etc/vpn2-control/api-managed')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pki').mkdir()
            (root / 'server.conf').write_text('dev tun0\nmanagement-client-user root\n')
            switch = switch.replace('/etc/openvpn/server', directory)
            result = bash(source('infra/lib/anysda-svc.sh') + f'''
HOST_TAG=ru
SVC_FP_DIR={directory}/applied
OVPN_DIR={directory}
PKI={directory}/pki
systemctl() {{ [[ $1 == restart ]] && echo "restart $2" >> {directory}/restarts; return 0; }}
echo deploy
{stage}
echo panel
{switch}
echo repeat
{stage}
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('management-client-user vpn2-control', (root / 'server.conf').read_text())
            restarts = (root / 'restarts').read_text().splitlines()
            self.assertEqual(restarts, ['restart openvpn-server@server'] * 2, result.stdout)
            self.assertIn('не перезапускаю', result.stdout.split('repeat', 1)[1])


if __name__ == '__main__':
    unittest.main()
