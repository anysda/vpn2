"""OpenVPN last good is not older than the live server.conf after 29-openvpn and after restore.

29-openvpn writes server.conf itself, and only 29-control records it as last good; restore lays
/etc/openvpn from the archive and starts openvpn. A deploy failing between them, or a restore,
left a last good older than what runs: a rollback or a failed install brought back the old port.
The stage, 29-control and restore run as they are, cut from the scripts; vpn2-apply is the real
apply.py on the fake host of test_apply, rooted in the test directory.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT, source

OPENVPN = '/usr/sbin/openvpn'
# Unit inactive while the live server.conf holds the text of work/down: for the stage's
# systemctl and for vpn2-apply alike.
WRAPPER = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from test_apply import Host, apply
root, state, work = (Path(sys.argv.pop(1)) for _ in range(3))
host = Host(root)


def command(argv, data=None):
    down = work / 'down'
    if argv[:2] == ['systemctl', 'is-active'] and argv[-1] == 'openvpn-server@server' and down.exists() \\
            and down.read_text() in (root / 'etc/openvpn/server/server.conf').read_text():
        raise RuntimeError('systemctl is-active: inactive')
    return host(argv, data)


real = apply.Applier.__init__
apply.Applier.__init__ = lambda self: real(self, root, state, command, 0)
apply.main()
'''


def between(text, start, end):
    return text[text.index(start):text.index(end, text.index(start))]


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnGoodAfterStageTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.root = self.work / 'root'
        self.state = self.root / 'var/lib/vpn2-apply'
        self.state.mkdir(parents=True)
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
                                   f'{self.root} {self.state} {self.work} "$@"\n')
        self.vpn2_apply.chmod(0o755)

    def rooted(self, text):
        return text.replace('/usr/local/sbin/vpn2-apply', str(self.vpn2_apply)) \
            .replace('/etc/', f'{self.root}/etc/').replace('/run/', f'{self.root}/run/') \
            .replace('/var/lib/', f'{self.root}/var/lib/')

    def header(self, port=1194):
        return source('infra/lib/anysda-svc.sh') + f'''
HOST_TAG=ru
SVC_FP_DIR={self.work}/applied
OVPN_PORT={port} OVPN_PROTO=udp OVPN_SERVER_PROTO=udp OVPN_SUBNET=10.67.67.0 OVPN_SUBNETS=10.67.67.0/24
OVPN_DIR={self.base}
PKI=$OVPN_DIR/pki
PATH={self.work}/bin:$PATH
systemctl() {{
  if [[ $1 == is-active || $1 == restart ]] && [[ -s {self.work}/down ]] \\
      && grep -qF -- "$(< {self.work}/down)" {self.base}/server.conf; then
    return 3
  fi
  return 0
}}
sysctl() {{ :; }}
ufw_sync_rules() {{ :; }}
ufw_sync_dns() {{ :; }}
id() {{ return 1; }}
docker() {{ :; }}
swanctl() {{ :; }}
'''

    def bash(self, script):
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.work,
                              text=True, capture_output=True, timeout=60)

    def deploy(self, port, control=True):
        """29-openvpn with the port; then 29-control records last good, unless a stage between fails."""
        stage = source('infra/scripts/29-openvpn.sh')
        steps = between(stage, 'OVPN_CONTROL_USER=', 'mkdir -p /var/anysda/.stamps') \
            + between(stage, '# ── 4. server.conf', '# ── 6. tun0')
        if control:
            steps += 'echo "== 29-control"\n' + between(
                source('infra/scripts/29-control.sh'), 'for resource in sing-box openvpn ikev2 wg; do',
                'install -o root -g root -m 0644 "$CONTROL_SOURCE/vpn2-control.service"') \
                .replace('for resource in sing-box openvpn ikev2 wg; do', 'for resource in openvpn; do')
        return self.bash(self.header(port) + 'echo "== 29-openvpn"\n' + self.rooted(steps))

    def deployed(self, port, control=True):
        result = self.deploy(port, control)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f'port {port}\n', self.live())
        return result.stdout + result.stderr

    def restore(self, conf):
        """anysda-restore from the PKI unpacking on: the archive holds this server.conf."""
        bundle = self.work / 'bundle/etc-openvpn/server'
        (bundle / 'ccd').mkdir(parents=True)
        (bundle / 'pki').mkdir()
        for name in ('ca.crt', 'server.crt', 'server.key', 'tls-crypt.key', 'crl.pem'):
            (bundle / 'pki' / name).write_text(name + '\n')
        (bundle / 'server.conf').write_text(conf)
        (self.work / 'restore-work').mkdir()
        lib = source('infra/lib/anysda-restore.sh')
        block = between(lib, 'copy_into() {', 'rollback_path() {') \
            + between(lib, 'start_unit() {', '# ── Остановка сервисов') \
            + between(lib, 'STEP="распаковка PKI OpenVPN"', '# ── Smoke-test через 99-verify')
        result = self.bash(self.header() + f'''
BUNDLE_DIR={self.work}/bundle
WORK={self.work}/restore-work
SOURCE_ROOT={self.work}/no-source
CADDY_STORAGE={self.root}/var/lib/caddy/.local/share/caddy
CADDY_SYNC_UNITS=()
TOUCHED=()
''' + self.rooted(block) + '\necho RESTORED\n')
        self.assertIn('RESTORED', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.live(), conf, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def vpn2_apply_run(self, *argv):
        return subprocess.run([str(self.vpn2_apply), *argv], text=True, capture_output=True, timeout=60)

    def port(self):
        return next(line for line in self.live().splitlines() if line.startswith('port '))

    def live(self):
        return (self.base / 'server.conf').read_text()

    def good(self):
        return (self.state / 'openvpn.good').read_text()

    def rolled_back(self):
        rolled = self.vpn2_apply_run('rollback', 'openvpn')
        self.assertEqual(rolled.returncode, 3, rolled.stderr)
        return rolled.stderr

    def failed_install(self):
        broken = self.work / 'broken.conf'
        broken.write_text(self.live() + 'push "BROKEN"\n')
        failed = self.vpn2_apply_run('install', 'openvpn', str(broken))
        self.assertEqual(failed.returncode, 3, failed.stderr)
        self.assertIn('previous config restored', failed.stderr)
        self.assertNotIn('BROKEN', self.live())
        return failed.stderr

    def stage_failed_after_29_openvpn(self):
        self.deployed(1194)
        self.assertIn('port 1194\n', self.good())
        return self.deployed(1195, control=False)

    def test_1_rollback_after_deploy_failed_past_29_openvpn_keeps_new_port(self):
        """1 29-openvpn 1194 -> 1195, a later stage fails: rollback keeps 1195."""
        output = self.stage_failed_after_29_openvpn()
        output += self.rolled_back()
        self.assertEqual(self.port(), 'port 1195', output)

    def test_1_failed_install_after_deploy_failed_past_29_openvpn_keeps_new_port(self):
        """1 29-openvpn 1194 -> 1195, a later stage fails: a rejected install returns to 1195."""
        output = self.stage_failed_after_29_openvpn()
        output += self.failed_install()
        self.assertEqual(self.port(), 'port 1195', output)

    def test_2_failed_install_after_full_deploy_returns_last_good(self):
        """2 full deploys 1194 then 1195: last good is 1195, a failed install returns to it."""
        self.deployed(1194)
        output = self.deployed(1195)
        self.assertEqual(self.good(), self.live(), output)
        good = self.good()
        output += self.failed_install()
        self.assertEqual(self.live(), good, output)
        self.assertIn('port 1195\n', self.live(), output)

    def archive_conf(self):
        self.deployed(1194)
        conf = self.live().replace('port 1194\n', 'port 1195\n')
        self.assertNotEqual(conf, self.live())
        return conf

    def test_3_rollback_after_restore_keeps_archive_conf(self):
        """3 restore of an archive with port 1195 over a live 1194: rollback keeps the archive's."""
        conf = self.archive_conf()
        output = self.restore(conf)
        output += self.rolled_back()
        self.assertEqual(self.port(), 'port 1195', output)
        self.assertEqual(self.live(), conf, output)

    def test_3_failed_install_after_restore_keeps_archive_conf(self):
        """3 restore of an archive with port 1195 over a live 1194: a failed install returns to it."""
        conf = self.archive_conf()
        output = self.restore(conf)
        output += self.failed_install()
        self.assertEqual(self.port(), 'port 1195', output)
        self.assertEqual(self.live(), conf, output)

    def test_4_29_openvpn_failing_itself_leaves_the_working_conf_after_rollback(self):
        """4 openvpn does not come up with 1195: the stage fails, rollback returns to 1194."""
        self.deployed(1194)
        old = self.live()
        (self.work / 'down').write_text('port 1195\n')
        result = self.deploy(1195)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertEqual(self.good(), old, output)
        output += self.rolled_back()
        self.assertEqual(self.live(), old, output)

    def test_4_first_install_without_good_keeps_the_new_conf(self):
        """4 first install, a later stage fails: rollback leaves the stage's conf, the next deploy marks it."""
        output = self.deployed(1194, control=False)
        new = self.live()
        self.vpn2_apply_run('rollback', 'openvpn')
        self.assertEqual(self.live(), new, output)
        output += self.deployed(1194)
        self.assertEqual(self.good(), self.live(), output)


if __name__ == '__main__':
    unittest.main()
