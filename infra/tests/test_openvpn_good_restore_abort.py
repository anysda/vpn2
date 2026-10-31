"""A restore that fails after openvpn came up and rolls back leaves last good at the pre-restore conf.

The archive holds port 1194 over a live and last good 1195. A unit started after openvpn does not
start: restore returns /etc/openvpn from its safety copy and exits. Last good must stay the conf
that ran before restore, or a failed install and a rollback bring back the cancelled archive's.
The restore transaction runs as it is, cut from anysda-restore.sh with its own rollback.
"""
import unittest

import test_openvpn_good_after_stage as stage
from test_deploy import source

between = stage.between


class OpenvpnGoodRestoreAbortTests(stage.OpenvpnGoodAfterStageTests):
    def aborted_restore(self, unit):
        """Full deploy on 1195, then restore of a 1194 archive where `systemctl start <unit>` fails once."""
        self.deployed(1195)
        before = self.live()
        self.assertEqual(self.good(), before)
        archive = before.replace('port 1195\n', 'port 1194\n')
        bundle = self.work / 'bundle/etc-openvpn/server'
        (bundle / 'ccd').mkdir(parents=True)
        (bundle / 'pki').mkdir()
        for name in ('ca.crt', 'server.crt', 'server.key', 'tls-crypt.key', 'crl.pem'):
            (bundle / 'pki' / name).write_text(name + '\n')
        (bundle / 'server.conf').write_text(archive)
        (self.work / 'restore-work').mkdir()
        lib = source('infra/lib/anysda-restore.sh')
        paths = between(lib, 'rollback_path() {', 'rollback() {').replace('"/$', f'"{self.root}/$')
        block = between(lib, 'copy_into() {', 'rollback_path() {') + paths \
            + between(lib, 'rollback() {', '# ── Остановка сервисов') \
            + between(lib, 'STEP="распаковка PKI OpenVPN"', 'trap - ERR INT TERM\nset +E')
        presnap = self.work / 'presnap'
        result = self.bash(self.header() + f'''
BUNDLE_DIR={self.work}/bundle
WORK={self.work}/restore-work
SOURCE_ROOT={self.work}/no-source
CADDY_STORAGE={self.root}/var/lib/caddy/.local/share/caddy
CADDY_SYNC_UNITS=()
TOUCHED=() DB_SWAPPED=0 CONTROL_STOPPED=0
PRESNAP_ROOT={presnap} PRESNAP_FINAL={presnap}.tar.gz.age
mkdir -p {presnap}/etc && cp -a {self.root}/etc/openvpn {presnap}/etc/
id() {{ [[ $1 == vpn2-control ]]; }}
systemctl() {{
  if [[ $1 == start && " $* " == *" {unit} "* && ! -e {self.work}/start-failed ]]; then
    touch {self.work}/start-failed
    echo "{unit}: start failed" >&2
    return 1
  fi
  return 0
}}
''' + self.rooted(block) + '\necho RESTORED\n')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 10, output)
        self.assertNotIn('RESTORED', result.stdout, output)
        self.assertIn('anysda-restore: FAILED на шаге', output)
        self.assertIn(unit, output)
        self.assertIn('возвращено из страховочной копии: /etc/openvpn', output)
        self.assertEqual(self.live(), before, output)
        return before, output

    def check_failed_install(self, unit):
        before, output = self.aborted_restore(unit)
        self.assertIn('port 1195\n', self.good(), output)
        self.assertEqual(self.good(), before, output)
        failed = self.vpn2_apply_run('install', 'openvpn', str(self.broken()))
        output += failed.stderr
        self.assertEqual(failed.returncode, 3, output)
        self.assertEqual(self.port(), 'port 1195', output)
        self.assertEqual(self.live(), before, output)

    def check_rollback(self, unit):
        before, output = self.aborted_restore(unit)
        output += self.vpn2_apply_run('rollback', 'openvpn').stderr
        self.assertEqual(self.port(), 'port 1195', output)
        self.assertEqual(self.live(), before, output)

    def broken(self):
        broken = self.work / 'broken.conf'
        broken.write_text(self.live() + 'push "BROKEN"\n')
        return broken

    def test_5_strongswan_fails_after_openvpn_failed_install_keeps_pre_restore_conf(self):
        """5 strongswan-starter does not start: restore rolls back, a failed install returns to 1195."""
        self.check_failed_install('strongswan-starter')

    def test_5_strongswan_fails_after_openvpn_rollback_keeps_pre_restore_conf(self):
        """5 strongswan-starter does not start: restore rolls back, rollback openvpn keeps 1195."""
        self.check_rollback('strongswan-starter')

    def test_5_vpn2_control_fails_last_failed_install_keeps_pre_restore_conf(self):
        """5 vpn2-control, the last unit before `trap -`, does not start: a failed install returns to 1195."""
        self.check_failed_install('vpn2-control.socket')

    def test_5_vpn2_control_fails_last_rollback_keeps_pre_restore_conf(self):
        """5 vpn2-control, the last unit before `trap -`, does not start: rollback openvpn keeps 1195."""
        self.check_rollback('vpn2-control.socket')


# Edges 1-4 run in test_openvpn_good_after_stage: only the helpers are inherited.
for inherited in dir(stage.OpenvpnGoodAfterStageTests):
    if inherited.startswith('test_'):
        setattr(OpenvpnGoodRestoreAbortTests, inherited, None)


if __name__ == '__main__':
    unittest.main()
