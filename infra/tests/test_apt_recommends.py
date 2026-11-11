"""Required stage packages survive disabled APT Recommends and partial removal."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from unittest import mock

from test_deploy import source
from vpn_roles import package


IKEV2_PACKAGES = (
    'strongswan', 'strongswan-starter', 'strongswan-charon', 'strongswan-pki',
    'strongswan-swanctl', 'libcharon-extauth-plugins', 'libcharon-extra-plugins',
    'libstrongswan-standard-plugins', 'libstrongswan-extra-plugins',
)


class AptRecommendsTests(unittest.TestCase):
    def run_packages(self, missing):
        """Шаг `ikev2-packages` роли: вызовы apt-get при недостающем пакете missing."""
        host, apt = package('cli.host'), package('apt')
        calls = []

        def query(argv, **_kwargs):
            states = ['config-files' if missing in (name, 'all') or name == 'charon-systemd' else 'installed'
                      for name in argv[3:]]
            return subprocess.CompletedProcess(argv, 0, '\n'.join(states) + '\n')

        def get(args, **_kwargs):
            calls.append(args)
            return 0
        with mock.patch.object(apt, '_run', query), mock.patch.object(apt, 'apt_get', get), \
                mock.patch.object(apt.install, '__kwdefaults__', dict(apt.install.__kwdefaults__, get=get)), \
                mock.patch.object(apt, 'update_if_stale', lambda **_kwargs: 0):
            changed = host._ikev2_packages(None)
        return changed, calls

    def test_fresh_ikev2_explicitly_installs_daemon_and_plugins(self):
        changed, calls = self.run_packages('all')
        self.assertTrue(changed)
        for name in IKEV2_PACKAGES:
            self.assertIn(name, calls[0], calls)

    def test_ikev2_repairs_each_missing_package_with_starter_installed(self):
        for name in IKEV2_PACKAGES:
            with self.subTest(package=name):
                changed, calls = self.run_packages(name)
                self.assertTrue(changed)
                self.assertEqual(calls[0][0], 'install')
                self.assertIn(name, calls[0], 'missing dependency was not repaired')

    def test_complete_ikev2_skips_apt(self):
        self.assertEqual(self.run_packages('none'), (False, []))
        self.assertIn("ikev2_host + ['ikev2-packages']", source('infra/ansible/roles/ikev2/tasks/main.yml'))

    def test_bootstrap_repairs_required_packages(self):
        # Доустановку недостающего ведёт шаг apt пакета vpn2; роль отдаёт ему
        # полный список, и пакеты IKEv2 в нём названы поимённо.
        defaults = source('infra/ansible/roles/base/defaults/main.yml')
        common = defaults.split('base_packages_common:', 1)[1].split('base_packages_entry:', 1)[0]
        entry = defaults.split('base_packages_entry:', 1)[1].split('base_packages_zapret:', 1)[0]
        self.assertIn('  - openssh-client\n', common)
        for name in IKEV2_PACKAGES:
            with self.subTest(package=name):
                self.assertIn(f'  - {name}\n', entry, 'bootstrap omitted required package')
        self.assertIn('+ (base_packages_entry if base_entry else [])', defaults)

    def test_restored_plugin_restarts_charon_but_unchanged_repeat_does_not(self):
        ikev2, svc = package('ikev2'), package('svc')
        self.assertIn('/usr/lib/ipsec/plugins/*.so', ikev2.STARTER_INPUTS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugins = root / 'usr/lib/ipsec/plugins'
            plugins.mkdir(parents=True)
            (plugins / 'libstrongswan-vici.so').write_text('vici')
            (root / 'bin').mkdir()
            fake = root / 'bin/systemctl'
            fake.write_text(f'#!/bin/sh\n[ "$1" != restart ] || echo restart >> {root}/calls\n')
            fake.chmod(0o755)
            inputs = [directory + pattern for pattern in ikev2.STARTER_INPUTS]

            def restart():
                with mock.patch.object(ikev2, 'STARTER_INPUTS', inputs):
                    return svc.restart_if_changed(ikev2.UNIT, ikev2.starter_inputs(), root / 'applied')
            with mock.patch.dict(os.environ, PATH=f'{root}/bin:{os.environ["PATH"]}'):
                self.assertEqual([restart(), restart()], [True, False])
                (plugins / 'libstrongswan-eap-mschapv2.so').write_text('plugin')
                self.assertEqual([restart(), restart()], [True, False])
            self.assertEqual((root / 'calls').read_text().splitlines(), ['restart', 'restart'])


if __name__ == '__main__':
    unittest.main()
