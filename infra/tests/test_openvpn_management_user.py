"""The panel's OpenVPN management user goes through vpn2-apply: last good keeps vpn2-control."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT, source

OPENVPN = '/usr/sbin/openvpn'
USER = 'management-client-user vpn2-control'
# The real apply.py on the fake host of test_apply, rooted in the test directory.
WRAPPER = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from test_apply import Host, apply
root, state, restarts = (Path(sys.argv.pop(1)) for _ in range(3))
host = Host(root)


def command(argv, data=None):
    if argv[:2] == ['systemctl', 'restart']:
        with restarts.open('a') as log:
            log.write(f'restart {argv[2]}\\n')
    return host(argv, data)


real = apply.Applier.__init__
apply.Applier.__init__ = lambda self: real(self, root, state, command, 0)
apply.main()
'''


def between(text, start, end):
    return text[text.index(start):text.index(end, text.index(start))]


def management(text):
    return [line for line in text.splitlines() if line.startswith('management-client-user')]


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnManagementUserTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.root = self.work / 'root'
        self.state = self.work / 'state'
        self.state.mkdir()
        self.base = self.root / 'etc/openvpn/server'
        (self.base / 'pki').mkdir(parents=True)
        (self.base / 'ccd').mkdir()
        (self.root / 'etc/vpn2-control').mkdir(parents=True)
        # The fake host looks at sing-box for every unit it shows.
        (self.root / 'etc/sing-box').mkdir(parents=True)
        (self.root / 'etc/sing-box/config.json').write_text('{"inbounds": []}')
        for name in ('ca.crt', 'server.crt', 'server.key', 'tls-crypt.key', 'crl.pem'):
            (self.base / 'pki' / name).write_text(name + '\n')
        (self.work / 'bin').mkdir()
        (self.work / 'apply.py').write_text(WRAPPER)
        self.vpn2_apply = self.work / 'bin/vpn2-apply'
        self.vpn2_apply.write_text(f'#!/bin/sh\nexec python3 {self.work}/apply.py {ROOT}/infra/tests '
                                   f'{self.root} {self.state} {self.work}/restarts "$@"\n')
        self.vpn2_apply.chmod(0o755)
        self.restarts = self.work / 'restarts'
        self.restarts.touch()

    def rooted(self, text):
        return text.replace('/usr/local/sbin/vpn2-apply', str(self.vpn2_apply)) \
            .replace('/etc/', f'{self.root}/etc/').replace('/run/', f'{self.root}/run/')

    def deploy(self):
        """29-openvpn, then 29-control records last good, then 30-frontend switches the user."""
        stage = source('infra/scripts/29-openvpn.sh')
        control = source('infra/scripts/29-control.sh')
        panel = source('infra/scripts/30-frontend.sh')
        steps = (
            between(stage, 'OVPN_CONTROL_USER=', 'mkdir -p /var/anysda/.stamps')
            + between(stage, '# ── 4. server.conf', 'svc_settle 2')
            + 'echo "== 29-control"\n'
            + between(control, 'for resource in sing-box openvpn ikev2 wg; do',
                      'install -o root -g root -m 0644 "$CONTROL_SOURCE/vpn2-control.service"')
            .replace('for resource in sing-box openvpn ikev2 wg; do', 'for resource in openvpn; do')
            + 'echo "== 30-frontend"\n'
            + between(panel, 'systemctl stop anysda-ikev2-sync.timer', 'systemctl start anysda-ikev2-sync.timer'))
        script = source('infra/lib/anysda-svc.sh') + f'''
HOST_TAG=ru
SVC_FP_DIR={self.work}/applied
OVPN_PORT=1194 OVPN_PROTO=udp OVPN_SERVER_PROTO=udp OVPN_SUBNET=10.67.67.0 OVPN_SUBNETS=10.67.67.0/24
OVPN_DIR={self.base}
PKI=$OVPN_DIR/pki
PATH={self.work}/bin:$PATH
systemctl() {{ [[ $1 == restart ]] && echo "restart $2" >> {self.restarts}; return 0; }}
sysctl() {{ :; }}
ufw_sync_rules() {{ :; }}
ufw_sync_dns() {{ :; }}
echo "== 29-openvpn"
''' + self.rooted(steps)
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.work,
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def vpn2_apply_run(self, *argv):
        return subprocess.run([str(self.vpn2_apply), *argv], text=True, capture_output=True, timeout=60)

    def live(self):
        return (self.base / 'server.conf').read_text()

    def good(self):
        return (self.state / 'openvpn.good').read_text()

    def assert_both_switched(self, output=''):
        self.assertEqual(management(self.live()), [USER], output)
        self.assertEqual(management(self.good()), [USER], output)
        self.assertEqual(self.good(), self.live(), output)

    def test_fresh_deploy_records_the_switched_user_as_last_good(self):
        output = self.deploy()
        self.assert_both_switched(output)

    def test_rollback_and_failed_install_keep_vpn2_control(self):
        self.deploy()
        rolled = self.vpn2_apply_run('rollback', 'openvpn')
        self.assertEqual(rolled.returncode, 3, rolled.stderr)
        self.assertEqual(management(self.live()), [USER], rolled.stderr)
        broken = self.work / 'broken.conf'
        broken.write_text(self.live() + 'push "BROKEN"\n')
        failed = self.vpn2_apply_run('install', 'openvpn', str(broken))
        self.assertEqual(failed.returncode, 3, failed.stderr)
        self.assertIn('previous config restored', failed.stderr)
        self.assertEqual(management(self.live()), [USER], failed.stderr)
        self.assertNotIn('BROKEN', self.live())

    def test_repeat_deploy_changes_nothing_and_does_not_restart(self):
        self.deploy()
        live, good = self.live(), self.good()
        self.restarts.write_text('')
        output = self.deploy()
        self.assertEqual(self.live(), live, output)
        self.assertEqual(self.good(), good, output)
        self.assertEqual(self.restarts.read_text(), '', output)
        self.assertIn('не перезапускаю', output)

    def test_old_node_with_root_in_live_and_good_comes_to_vpn2_control(self):
        conf = self.base / 'server.conf'
        conf.write_text('port 1194\nproto udp\ndev tun0\nca pki/ca.crt\ncert pki/server.crt\n'
                        'key pki/server.key\ndh none\ntls-crypt pki/tls-crypt.key\n'
                        'crl-verify pki/crl.pem\nclient-config-dir ccd\n'
                        f'management {self.base}/mgmt.sock unix\nmanagement-client-user root\n')
        (self.state / 'openvpn.good').write_text(conf.read_text())
        output = self.deploy()
        self.assert_both_switched(output)

    def test_node_switched_by_sed_heals_last_good_on_next_deploy(self):
        # Left by the sed switch: live vpn2-control, last good still root.
        self.deploy()
        (self.state / 'openvpn.good').write_text(self.live().replace(USER, 'management-client-user root'))
        self.assertTrue((self.root / 'etc/vpn2-control/api-managed').exists())
        output = self.deploy()
        self.assert_both_switched(output)

    def header(self):
        return source('infra/lib/anysda-svc.sh') + f'''
HOST_TAG=ru
SVC_FP_DIR={self.work}/applied
OVPN_DIR={self.base}
PATH={self.work}/bin:$PATH
systemctl() {{ [[ $1 == *restart ]] && echo "$1 $2" >> {self.restarts}; return 0; }}
'''

    def fingerprint(self):
        script = self.header() + 'ovpn_inputs; svc_fingerprint "${OVPN_INPUTS[@]}"'
        return subprocess.run(['bash', '-c', script], text=True, capture_output=True, check=True).stdout

    def recorded_fingerprint(self):
        return (self.work / 'applied/openvpn-server@server.sha256').read_text()

    def marker(self):
        return self.root / 'etc/vpn2-control/api-managed'

    def old_root_node(self):
        """Live and last good with root, no marker, fingerprint of what runs."""
        self.deploy()
        conf = self.live().replace(USER, 'management-client-user root')
        (self.base / 'server.conf').write_text(conf)
        (self.state / 'openvpn.good').write_text(conf)
        self.marker().unlink()
        (self.work / 'applied/openvpn-server@server.sha256').write_text(self.fingerprint())
        self.restarts.write_text('')

    def failing_frontend(self):
        """30-frontend under its panel transaction; the new panel never passes the health check."""
        panel = source('infra/scripts/30-frontend.sh')
        txn = source('infra/lib/panel-transaction.sh').replace(
            'panel_lib=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)', 'panel_lib=/nonexistent')
        script = self.header() + txn + f'''
panel_state={self.work}/panel
curl() {{ return 7; }}
sleep() {{ :; }}
''' + self.rooted(between(panel, 'panel_files_restored() {', 'TLS_PATHS=')) + '''
panel_changed=true
trap panel_failure EXIT
''' + self.rooted(between(panel, 'systemctl stop anysda-ikev2-sync.timer',
                          'systemctl start anysda-ikev2-sync.timer')) + 'panel_health\n'
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.work,
                                text=True, capture_output=True, timeout=60)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_panel_failure_after_switch_keeps_live_and_good_together(self):
        self.old_root_node()
        output = self.failing_frontend()
        live = management(self.live())
        self.assertEqual(management(self.good()), live, output)
        self.assertEqual(self.marker().exists(), live == [USER], output)
        self.assertEqual(self.recorded_fingerprint(), self.fingerprint(), output)
        rolled = self.vpn2_apply_run('rollback', 'openvpn')
        self.assertEqual(management(self.live()), live, rolled.stderr)
        self.restarts.write_text('')
        output = self.deploy()
        self.assert_both_switched(output)
        self.assertLessEqual(len(self.restarts.read_text().splitlines()), 1, output)
        self.restarts.write_text('')
        output = self.deploy()
        self.assertEqual(self.restarts.read_text(), '', output)

    def test_panel_failure_without_switch_leaves_openvpn_alone(self):
        self.deploy()
        live, good, fp = self.live(), self.good(), self.recorded_fingerprint()
        output = self.failing_frontend()
        self.assertEqual(self.live(), live, output)
        self.assertEqual(self.good(), good, output)
        self.assertTrue(self.marker().exists(), output)
        self.assertEqual(self.recorded_fingerprint(), fp, output)

    def test_denied_switch_fails_the_stage_and_touches_nothing(self):
        self.old_root_node()
        live, fp = self.live(), self.recorded_fingerprint()
        self.vpn2_apply.write_text('#!/bin/sh\necho "vpn2-apply: Permission denied" >&2\nexit 1\n')
        output = self.failing_frontend()
        self.assertIn('vpn2-apply install openvpn: код 1, server.conf не изменён', output)
        self.assertEqual(self.live(), live, output)
        self.assertEqual(self.good(), live, output)
        self.assertFalse(self.marker().exists(), output)
        self.assertEqual(self.recorded_fingerprint(), fp, output)


if __name__ == '__main__':
    unittest.main()
