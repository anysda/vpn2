"""vpn2-apply install openvpn failing after it replaced server.conf: the stage rollback restarts OpenVPN."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import source

RESTART = 'try-restart openvpn-server@server'
ROOT_CONF = ('port 1194\nproto udp\ndev tun0\n'
             'management /run/openvpn/mgmt.sock unix\nmanagement-client-user root\n')
# RC=2: the candidate is rejected, server.conf untouched. Any other RC: server.conf is
# already replaced and OpenVPN restarted on it when apply dies (signal in the settle window).
APPLY = '''#!/bin/sh
echo "$*" >> {log}
[ "$1" = install ] || exit 0
[ "$RC" = 2 ] && {{ echo "vpn2-apply: openvpn: candidate rejected" >&2; exit 2; }}
cat "$3" > {live}
exit "$RC"
'''


def between(text, start, end):
    return text[text.index(start):text.index(end, text.index(start))]


class FrontendFailureOpenvpnApplyRcTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.root = self.work / 'root'
        self.conf = self.root / 'etc/openvpn/server/server.conf'
        self.conf.parent.mkdir(parents=True)
        self.conf.write_text(ROOT_CONF)
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

    def switch_failing(self, rc):
        """The server.conf switch of 30-frontend under its panel transaction, apply exiting RC."""
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
''' + self.rooted(between(panel, 'systemctl stop anysda-ikev2-sync.timer',
                          'systemctl start anysda-ikev2-sync.timer')) + 'echo "switch passed" >&2\n'
        env = {'PATH': '/usr/bin:/bin', 'RC': str(rc)}
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.work, env=env,
                                text=True, capture_output=True, timeout=60)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, rc, output)
        self.assertNotIn('switch passed', output)
        self.assertIn('panel host config restored', output)
        self.assertIn('install openvpn', self.applied.read_text(), output)
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        self.assertIn('reload-or-restart caddy', calls, output)
        return calls, output

    def test_apply_killed_after_switch_restores_server_conf_and_restarts_openvpn(self):
        before = self.conf.read_bytes()
        calls, output = self.switch_failing(143)
        self.assertEqual(self.conf.read_bytes(), before, output)
        self.assertIn(RESTART, calls, output)

    def test_rejected_candidate_leaves_openvpn_running(self):
        before = self.conf.read_bytes()
        calls, output = self.switch_failing(2)
        self.assertEqual(self.conf.read_bytes(), before, output)
        self.assertNotIn(RESTART, calls, output)


if __name__ == '__main__':
    unittest.main()
