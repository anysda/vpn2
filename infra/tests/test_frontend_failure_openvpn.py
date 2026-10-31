"""A failed 30-frontend restarts OpenVPN only when its rollback put back a changed server.conf."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import source

RESTART = 'try-restart openvpn-server@server'
ROOT_CONF = ('port 1194\nproto udp\ndev tun0\n'
             'management /run/openvpn/mgmt.sock unix\nmanagement-client-user root\n')
APPLY = '''#!/bin/sh
echo "$*" >> {log}
[ "$1" = install ] || exit 0
[ -n "$DENY" ] && {{ echo "vpn2-apply: Permission denied" >&2; exit 1; }}
cat "$3" > {live}
'''


def between(text, start, end):
    return text[text.index(start):text.index(end, text.index(start))]


class FrontendFailureOpenvpnTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.root = self.work / 'root'
        self.conf = self.root / 'etc/openvpn/server/server.conf'
        self.conf.parent.mkdir(parents=True)
        (self.root / 'etc/vpn2-control').mkdir(parents=True)
        (self.root / 'etc/caddy').mkdir(parents=True)
        (self.root / 'etc/caddy/Caddyfile').write_text(':80 {\n}\n')
        (self.root / 'etc/systemd/system').mkdir(parents=True)
        (self.root / 'etc/systemd/system/vpn2-tls.timer').write_text('[Timer]\n')
        self.calls = self.work / 'systemctl'
        self.applied = self.work / 'vpn2-apply.log'
        self.vpn2_apply = self.work / 'vpn2-apply'
        self.vpn2_apply.write_text(APPLY.format(log=self.applied, live=self.conf))
        self.vpn2_apply.chmod(0o755)

    def rooted(self, text):
        return text.replace('/usr/local/sbin/vpn2-apply', str(self.vpn2_apply)) \
            .replace('/etc/', f'{self.root}/etc/')

    def failing_frontend(self, after_guard, deny=False):
        """30-frontend under its panel transaction, failing right after AFTER_GUARD."""
        panel = source('infra/scripts/30-frontend.sh')
        txn = source('infra/lib/panel-transaction.sh').replace(
            'panel_lib=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)', 'panel_lib=/nonexistent')
        script = txn + f'''
HOST_TAG=ru
panel_state={self.work}/panel
systemctl() {{ echo "$*" >> {self.calls}; return 0; }}
ovpn_restarted_by_apply() {{ :; }}
''' + self.rooted(between(panel, 'panel_files_restored() {', 'TLS_PATHS=')) + '''
panel_changed=true
trap panel_failure EXIT
''' + after_guard + '\necho "stage failed" >&2\nexit 1\n'
        env = {'PATH': '/usr/bin:/bin', 'DENY': '1' if deny else ''}
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.work, env=env,
                                text=True, capture_output=True, timeout=60)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        output = result.stdout + result.stderr
        self.assertIn('panel host config restored', output)
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        self.assertIn('reload-or-restart caddy', calls, output)
        self.assertIn('start vpn2-tls.timer', calls, output)
        return calls, output

    def switch(self):
        panel = source('infra/scripts/30-frontend.sh')
        return self.rooted(between(panel, 'systemctl stop anysda-ikev2-sync.timer',
                                   'systemctl start anysda-ikev2-sync.timer'))

    def test_failure_before_server_conf_change_leaves_openvpn_running(self):
        self.conf.write_text(ROOT_CONF.replace('root', 'vpn2-control'))
        before = self.conf.read_bytes()
        calls, output = self.failing_frontend('')
        self.assertNotIn(RESTART, calls, output)
        self.assertEqual(self.conf.read_bytes(), before, output)

    def test_denied_switch_leaves_openvpn_running(self):
        self.conf.write_text(ROOT_CONF)
        before = self.conf.read_bytes()
        calls, output = self.failing_frontend(self.switch(), deny=True)
        self.assertIn('server.conf не изменён', output)
        self.assertNotIn(RESTART, calls, output)
        self.assertEqual(self.conf.read_bytes(), before, output)

    def test_failure_after_server_conf_change_restores_it_and_restarts_openvpn(self):
        self.conf.write_text(ROOT_CONF)
        before = self.conf.read_bytes()
        # The switch goes through, then the stage fails before the new panel is healthy.
        calls, output = self.failing_frontend(
            self.switch() + f'grep -qx "management-client-user vpn2-control" {self.conf}\n')
        self.assertIn('install openvpn', self.applied.read_text(), output)
        self.assertIn(RESTART, calls, output)
        self.assertEqual(self.conf.read_bytes(), before, output)


if __name__ == '__main__':
    unittest.main()
