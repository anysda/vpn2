"""Сторож фаервола стадий: без новых ssh-входов фаервол возвращается к снимку.

VPN2_TEST_ROOT позволяет прогнать те же проверки против другой ревизии.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
GUARD = ROOT / 'infra/lib/fw-guard.sh'

FAKE_UFW = '''#!/bin/bash
echo "ufw $*" >> "$FAKE/calls"
case "$1 $2" in
  'status '*|'status ') [ -e "$FAKE/ufw-active" ] && echo 'Status: active' || echo 'Status: inactive' ;;
  '--force enable') touch "$FAKE/ufw-active" ;;
  '--force disable') rm -f "$FAKE/ufw-active" ;;
esac
'''

FAKE_NFT = '''#!/bin/bash
echo "nft $*" >> "$FAKE/calls"
case "$1" in
  list) [ -f "$FAKE/table-$4" ] && cat "$FAKE/table-$4" || exit 1 ;;
  delete) rm -f "$FAKE/table-$4" ;;
  -f) name=$(sed -n 's/^table inet \\([a-z_]*\\) {$/\\1/p' "$2"); cp "$2" "$FAKE/table-$name" ;;
esac
'''

FAKE_LOG = '''#!/bin/bash
echo "$(basename "$0") $*" >> "$FAKE/calls"
'''


class FirewallGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / 'fake'
        self.bin = self.root / 'bin'
        self.etc = self.root / 'etc'
        self.dir = self.root / 'run/vpn2-fw-guard'
        for path in (self.fake, self.bin, self.etc / 'ufw', self.etc / 'default'):
            path.mkdir(parents=True)
        for name, body in (('ufw', FAKE_UFW), ('nft', FAKE_NFT),
                           ('systemd-run', FAKE_LOG), ('systemctl', FAKE_LOG)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.etc / 'ufw/user.rules').write_text('-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n')
        (self.etc / 'default/ufw').write_text('DEFAULT_INPUT_POLICY="DROP"\n')
        (self.fake / 'ufw-active').touch()
        (self.fake / 'table-ytzapret').write_text('table inet ytzapret {\n}\n')
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', FAKE=str(self.fake),
                        FW_GUARD_DIR=str(self.dir), FW_GUARD_ETC=str(self.etc))

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, *args):
        return subprocess.run(['bash', str(GUARD), *args], env=self.env, text=True,
                              capture_output=True, timeout=30)

    def calls(self):
        path = self.fake / 'calls'
        return path.read_text().splitlines() if path.exists() else []

    def break_firewall(self):
        (self.etc / 'ufw/user.rules').write_text('-A ufw-user-input -p tcp --dport 22 -j DROP\n')
        (self.fake / 'table-ytzapret').write_text('table inet ytzapret {\n  broken\n}\n')
        (self.fake / 'table-anysda_ytquic').write_text('table inet anysda_ytquic {\n}\n')

    def test_silence_longer_than_window_restores_snapshot_and_fails_disarm(self):
        result = self.guard('arm', '28-wireguard', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c.startswith('systemd-run ') and c.endswith(' watch 1') for c in self.calls()))
        self.break_firewall()
        stale = time.time() - 10
        os.utime(self.dir / 'alive', (stale, stale))

        watch = self.guard('watch', '1')
        self.assertEqual(watch.returncode, 0, watch.stdout + watch.stderr)
        self.assertIn('откатываю фаервол к снимку до стадии 28-wireguard', watch.stdout)
        self.assertIn('--dport 22 -j ACCEPT', (self.etc / 'ufw/user.rules').read_text())
        self.assertNotIn('broken', (self.fake / 'table-ytzapret').read_text())
        self.assertFalse((self.fake / 'table-anysda_ytquic').exists())
        self.assertIn('ufw reload', self.calls())

        disarm = self.guard('disarm', '28-wireguard')
        self.assertEqual(disarm.returncode, 3, disarm.stdout + disarm.stderr)
        self.assertIn('откатаны к состоянию до неё', disarm.stdout)
        self.assertFalse(self.dir.exists())

    def test_fresh_logins_keep_stage_changes(self):
        self.assertEqual(self.guard('arm', '29-openvpn', '3').returncode, 0)
        self.break_firewall()
        watch = subprocess.Popen(['bash', str(GUARD), 'watch', '3'], env=self.env,
                                 stdout=subprocess.PIPE, text=True)
        for _ in range(4):
            time.sleep(1.5)
            self.assertEqual(self.guard('beat').returncode, 0)
        disarm = self.guard('disarm', '29-openvpn')
        self.assertEqual(disarm.returncode, 0, disarm.stdout + disarm.stderr)
        self.assertEqual(watch.wait(timeout=10), 0)
        self.assertEqual(watch.stdout.read(), '')
        self.assertIn('-j DROP', (self.etc / 'ufw/user.rules').read_text())
        self.assertNotIn('ufw reload', self.calls())
        self.assertFalse(self.dir.exists())

    def test_firewall_without_ufw_before_stage_is_disabled_again(self):
        (self.bin / 'ufw').rename(self.root / 'ufw')
        self.assertEqual(self.guard('arm', '00-bootstrap', '1').returncode, 0)
        (self.root / 'ufw').rename(self.bin / 'ufw')
        os.utime(self.dir / 'alive', (time.time() - 10,) * 2)
        self.assertEqual(self.guard('watch', '1').returncode, 0)
        self.assertIn('ufw --force disable', self.calls())
        self.assertFalse((self.fake / 'ufw-active').exists())


def deploy_function(name):
    body = (ROOT / 'infra/deploy.sh').read_text().split(f'{name}() {{', 1)[1].split('\n}', 1)[0]
    return f'{name}() {{' + body + '\n}\n'


class DeployGuardTests(unittest.TestCase):
    def run_stage(self, stage, disarm_rc=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ('scripts', 'envs', 'lib'):
                (root / path).mkdir()
            (root / 'scripts' / f'{stage}.sh').touch()
            (root / 'envs/all.env').write_text('COMMON=1\n')
            (root / 'envs/ru.env').write_text('HOST_TAG=ru\n')
            source = (ROOT / 'infra/deploy.sh').read_text()
            settings = ''.join(line + '\n' for line in source.splitlines()
                               if line.startswith(('FW_GUARD_STAGES=', 'FW_GUARD_WINDOW=')))
            script = settings + ''.join(deploy_function(name) for name in (
                'fw_guard_stage', 'fw_guard_beat', 'fw_guard_disarm', 'run_stage_on_host')) + f'''
DEPLOY_ROOT=$PWD
HOST_TAG=ru
load_env() {{ :; }}
log() {{ :; }}
ok() {{ :; }}
warn() {{ echo "WARN $*"; }}
push() {{ echo "push $(basename "$1")"; }}
ssh_exec() {{ echo "exec $*"; }}
ssh_fresh() {{
  echo "fresh $*" >> fresh.log
  if [[ $* == *disarm* ]]; then echo 'vpn2-fw-guard: откатано'; return {disarm_rc}; fi
}}
if run_stage_on_host {stage} ru; then echo RESULT=0; else echo RESULT=1; fi
[[ ! -f fresh.log ]] || cat fresh.log
'''
            return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=directory,
                                  text=True, capture_output=True, timeout=30)

    def test_guarded_stage_arms_before_script_and_disarms_after(self):
        result = self.run_stage('28-wireguard')
        out = result.stdout
        self.assertIn('push fw-guard.sh', out)
        stage = next(line for line in out.splitlines() if '/tmp/anysda/28-wireguard.sh /tmp' in line)
        self.assertRegex(stage, r'fw-guard\.sh arm 28-wireguard \d+ \|\| exit 1; /tmp/anysda/28-wireguard\.sh')
        self.assertIn('fresh bash --norc /run/vpn2-fw-guard/fw-guard.sh disarm 28-wireguard', out)
        self.assertIn('RESULT=0', out)

    def test_rolled_back_firewall_fails_the_stage(self):
        result = self.run_stage('27-ikev2', disarm_rc=3)
        self.assertIn('WARN [27-ikev2 → ru] vpn2-fw-guard: откатано', result.stdout)
        self.assertIn('RESULT=1', result.stdout)

    def test_emergency_stage_is_guarded(self):
        result = self.run_stage('23-emergency')
        out = result.stdout
        self.assertIn('push fw-guard.sh', out)
        stage = next(line for line in out.splitlines() if '/tmp/anysda/23-emergency.sh /tmp' in line)
        self.assertRegex(stage, r'fw-guard\.sh arm 23-emergency \d+ \|\| exit 1; /tmp/anysda/23-emergency\.sh')
        self.assertIn('fresh bash --norc /run/vpn2-fw-guard/fw-guard.sh disarm 23-emergency', out)
        self.assertIn('RESULT=0', out)

    def test_rolled_back_emergency_firewall_fails_the_stage(self):
        result = self.run_stage('23-emergency', disarm_rc=3)
        self.assertIn('WARN [23-emergency → ru] vpn2-fw-guard: откатано', result.stdout)
        self.assertIn('RESULT=1', result.stdout)

    def test_stage_that_keeps_the_firewall_is_not_guarded(self):
        result = self.run_stage('99-verify')
        self.assertNotIn('fw-guard', result.stdout)
        self.assertIn('RESULT=0', result.stdout)

    def test_every_stage_that_changes_the_firewall_is_guarded(self):
        source = (ROOT / 'infra/deploy.sh').read_text()
        match = re.search(r"^FW_GUARD_STAGES='([^']*)'", source, re.M)
        self.assertIsNotNone(match, 'deploy.sh не охраняет фаервол стадий')
        guarded = set(match.group(1).split())
        changing = re.compile(r'^\s*[^#\n]*\b(ufw (allow|delete|default|--force|reload|deny)|ufw_sync_\w+|nft (-f|delete|add))\b', re.M)
        for script in sorted((ROOT / 'infra/scripts').glob('*.sh')):
            if changing.search(script.read_text()):
                self.assertIn(script.stem, guarded)


if __name__ == '__main__':
    unittest.main()
