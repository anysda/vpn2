"""Switching the OpenVPN management user does not restart OpenVPN again on the next deploy."""
from pathlib import Path
import tempfile
import unittest

from test_deploy import ROOT, bash, source
from test_openvpn_management_user import OPENVPN, WRAPPER


def between(text, start, end):
    return text[text.index(start):text.index(end, text.index(start))]


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnRestartTests(unittest.TestCase):
    def test_management_user_switch_keeps_fingerprint_for_next_deploy(self):
        stage = between(source('infra/scripts/29-openvpn.sh'),
                        'echo "[$HOST_TAG] [5/6] openvpn-server@server"', 'svc_settle 2')
        switch = between(source('infra/scripts/30-frontend.sh'),
                         'if [[ -f /etc/openvpn/server/server.conf ]]', 'touch /etc/vpn2-control/api-managed')
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            root = work / 'root'
            base = root / 'etc/openvpn/server'
            (base / 'pki').mkdir(parents=True)
            (base / 'ccd').mkdir()
            (work / 'state').mkdir()
            # The fake host of test_apply looks at sing-box for every unit it shows.
            (root / 'etc/sing-box').mkdir(parents=True)
            (root / 'etc/sing-box/config.json').write_text('{"inbounds": []}')
            for name in ('ca.crt', 'server.crt', 'server.key', 'tls-crypt.key', 'crl.pem'):
                (base / 'pki' / name).write_text(name + '\n')
            # A config the real vpn2-apply accepts as a candidate.
            (base / 'server.conf').write_text(
                'dev tun0\nca pki/ca.crt\ncert pki/server.crt\nkey pki/server.key\ndh none\n'
                'tls-crypt pki/tls-crypt.key\ncrl-verify pki/crl.pem\nclient-config-dir ccd\n'
                'management-client-user root\n')
            (work / 'apply.py').write_text(WRAPPER)
            vpn2_apply = work / 'vpn2-apply'
            vpn2_apply.write_text(f'#!/bin/sh\nexec python3 {work}/apply.py {ROOT}/infra/tests '
                                  f'{root} {work}/state {work}/restarts "$@"\n')
            vpn2_apply.chmod(0o755)
            switch = switch.replace('/usr/local/sbin/vpn2-apply', str(vpn2_apply)) \
                .replace('/etc/openvpn/server', str(base))
            result = bash(source('infra/lib/anysda-svc.sh') + f'''
HOST_TAG=ru
SVC_FP_DIR={work}/applied
OVPN_DIR={base}
PKI={base}/pki
systemctl() {{ [[ $1 == restart ]] && echo "restart $2" >> {work}/restarts; return 0; }}
echo deploy
{stage}
echo panel
{switch}
echo repeat
{stage}
''', work)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('management-client-user vpn2-control', (base / 'server.conf').read_text())
            restarts = (work / 'restarts').read_text().splitlines()
            self.assertEqual(restarts, ['restart openvpn-server@server'] * 2, result.stdout)
            self.assertIn('не перезапускаю', result.stdout.split('repeat', 1)[1])


if __name__ == '__main__':
    unittest.main()
