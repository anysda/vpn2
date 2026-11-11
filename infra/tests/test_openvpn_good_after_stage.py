"""OpenVPN last good is not older than the live server.conf after 29-openvpn and after restore.

29-openvpn writes server.conf itself, and only 29-control records it as last good; restore lays
/etc/openvpn from the archive and starts openvpn. A deploy failing between them, or a restore,
left a last good older than what runs: a rollback or a failed install brought back the old port.
The role's steps run from the vpn2 package, 29-control as cut from its script; vpn2-apply is the
real apply.py on the fake host of test_apply, rooted in the test directory.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT
import vpn_roles

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


# Что роль control делает после стадий VPN: доводит прерванный apply и
# записывает работающий конфиг как последний годный.
CONTROL_MARK = ('rc=0\n/usr/local/sbin/vpn2-apply resume openvpn || rc=$?\n[[ $rc == 0 || $rc == 3 ]] || exit "$rc"\n/usr/local/sbin/vpn2-apply mark openvpn >/dev/null 2>&1 || true\n')


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
        self.vpn2_apply.write_text(f'#!/bin/sh\nexec python3 {self.work}/apply.py {ROOT}/infra/vpn2/tests '
                                   f'{self.root} {self.state} {self.work} "$@"\n')
        self.vpn2_apply.chmod(0o755)
        # The role's steps call systemctl from PATH.
        systemctl = self.work / 'bin/systemctl'
        systemctl.write_text(f'''#!/bin/bash
if [[ $1 == is-active || $1 == restart ]] && [[ -s {self.work}/down ]] \\
    && grep -qF -- "$(< {self.work}/down)" {self.base}/server.conf; then
  exit 3
fi
exit 0
''')
        systemctl.chmod(0o755)

    def rooted(self, text):
        return text.replace('/usr/local/sbin/vpn2-apply', str(self.vpn2_apply)) \
            .replace('/etc/', f'{self.root}/etc/').replace('/run/', f'{self.root}/run/') \
            .replace('/var/lib/', f'{self.root}/var/lib/')

    def header(self):
        return f'''
HOST_TAG=ru
PATH={self.work}/bin:$PATH
'''

    def bash(self, script):
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=self.work,
                              text=True, capture_output=True, timeout=60)

    def deploy(self, port, control=True):
        """29-openvpn with the port; then 29-control records last good, unless a stage between fails."""
        steps = vpn_roles.ovpn_stage(self.root, self.work / 'applied', self.vpn2_apply, port)
        if control:
            steps += 'echo "== 29-control"\n' + self.rooted(CONTROL_MARK)
        return self.bash(self.header() + 'echo "== 29-openvpn"\n' + steps)

    def deployed(self, port, control=True):
        result = self.deploy(port, control)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f'port {port}\n', self.live())
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
