"""Required stage packages survive disabled APT Recommends and partial removal."""
from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, source


IKEV2_PACKAGES = (
    'strongswan', 'strongswan-starter', 'strongswan-charon', 'strongswan-pki',
    'strongswan-swanctl', 'libcharon-extauth-plugins', 'libcharon-extra-plugins',
    'libstrongswan-standard-plugins', 'libstrongswan-extra-plugins',
)


class AptRecommendsTests(unittest.TestCase):
    def run_packages(self, stage, missing, role='ru'):
        text = source(f'infra/scripts/{stage}.sh')
        if stage == '27-ikev2':
            block = text.split('# ── 1. apt strongswan', 1)[1]
            block = block[block.index('\n'):].split('# ── 2. Server certs', 1)[0]
        else:
            block = 'PKGS=(' + text.split('PKGS=(', 1)[1].split('\nnftables_preserve_rules', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            result = bash(source('infra/lib/anysda-svc.sh') + f'''
HOST_TAG={role}
MISSING_PACKAGE={missing}
YT_ROUTE=zapret
BACKUP_ENABLED=true
dpkg-query() {{
  shift 2
  for pkg in "$@"; do
    if [[ $pkg == "$MISSING_PACKAGE" || $MISSING_PACKAGE == all || $pkg == charon-systemd ]]; then
      echo config-files
    else
      echo installed
    fi
  done
}}
dpkg() {{ [[ $2 == strongswan-starter && $MISSING_PACKAGE != all ]]; }}
apt_get() {{ printf '%s\\n' "$*" >> calls; }}
apt_update_if_stale() {{ :; }}
docker_apt_source() {{ :; }}
''' + block, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = Path(directory) / 'calls'
            return calls.read_text() if calls.exists() else ''

    def test_fresh_ikev2_explicitly_installs_daemon_and_plugins(self):
        calls = self.run_packages('27-ikev2', 'all')
        for package in IKEV2_PACKAGES:
            self.assertIn(package, calls.split(), calls)

    def test_ikev2_repairs_each_missing_package_with_starter_installed(self):
        for package in IKEV2_PACKAGES:
            with self.subTest(package=package):
                calls = self.run_packages('27-ikev2', package)
                self.assertIn(package, calls.split(), 'missing dependency was not repaired')

    def test_complete_ikev2_skips_apt(self):
        self.assertEqual(self.run_packages('27-ikev2', 'none'), '')

    def test_bootstrap_repairs_required_packages(self):
        for package in (*IKEV2_PACKAGES, 'openssh-client'):
            with self.subTest(package=package):
                calls = self.run_packages('00-bootstrap', package)
                self.assertIn(package, calls.split(), 'bootstrap omitted required package')

    def test_restored_plugin_restarts_charon_but_unchanged_repeat_does_not(self):
        stage = source('infra/scripts/27-ikev2.sh')
        restart = 'svc_restart_if_changed strongswan-starter ' + stage.split(
            'svc_restart_if_changed strongswan-starter ', 1)[1].split('\n# Готов', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugins = root / 'usr/lib/ipsec/plugins'
            plugins.mkdir(parents=True)
            (plugins / 'libstrongswan-vici.so').write_text('vici')
            restart = restart.replace('/usr/', directory + '/usr/').replace('/etc/', directory + '/etc/')
            result = bash(source('infra/lib/anysda-svc.sh') + '''
SVC_FP_DIR=$PWD/fingerprints
systemctl() { [[ $1 != restart ]] || echo restart >> calls; }
''' + restart + '\n' + restart + '''
echo plugin > usr/lib/ipsec/plugins/libstrongswan-eap-mschapv2.so
''' + restart + '\n' + restart, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['restart', 'restart'])


if __name__ == '__main__':
    unittest.main()
