"""A swap file left by older deploys must not block the next one forever.

Entry 2 GB RAM / 10 GB disk: the old bootstrap made a 1962 MiB /swapfile, 1411
MiB stayed free and preflight stopped the deploy naming only the deploy
backups. Preflight runs as in test_release with a min_free_bytes no disk has;
the /swapfile it may look at is answered by a stat stub. The swap step itself
is covered in infra/vpn2/tests/test_host.py.
"""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from test_release import module


MIB = 1024 ** 2
GIB = 1024 ** 3
SWAPFILE = '/swapfile'


def fake_swapfile(size):
    """os.stat/os.lstat that answer for /swapfile only: a fully written file of size bytes."""
    real_stat, real_lstat = os.stat, os.lstat

    def answer(real):
        def stub(path, *args, **kwargs):
            try:
                name = os.fspath(path)
            except TypeError:
                name = None
            if name in (SWAPFILE, os.fsencode(SWAPFILE)):
                if size is None:
                    raise FileNotFoundError(2, 'No such file or directory', SWAPFILE)
                return os.stat_result((stat.S_IFREG | 0o600, 4242, 1, 1, 0, 0, size, 0, 0, 0),
                                      {'st_blocks': size // 512, 'st_blksize': 4096})
            return real(path, *args, **kwargs)
        return stub

    return patch.multiple(os, stat=answer(real_stat), lstat=answer(real_lstat))


class PreflightSwapfileTests(unittest.TestCase):
    def refusal(self, swap_size):
        state = module('deploy_state')
        release = module('release')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            item = work / 'backups/1000'
            item.mkdir(parents=True)
            (item / 'db.sqlite').write_bytes(b'x' * MIB)
            (work / 'os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
            (work / 'meminfo').write_text('MemAvailable: 4000000 kB\n')
            manifest = {'os': {'ubuntu': ['26.04']}, 'architectures': [release.architecture()],
                        'min_free_bytes': 2 ** 62, 'min_memory_bytes': 0}
            with fake_swapfile(swap_size), self.assertRaises(state.ReleaseError) as refused:
                state.preflight(manifest, work / 'db.sqlite', work, work / 'os-release', work / 'meminfo')
            message = str(refused.exception)
            print('deploy stopped: ' + message)
            return message, state.backup_usage(work)

    def test_1_refusal_names_swapfile_size_and_swapoff(self):
        message, _ = self.refusal(1962 * MIB)
        self.assertIn('services unchanged', message)
        self.assertIn('deploy backups:', message)
        self.assertIn('/swapfile', message, 'disk-space refusal says nothing about /swapfile')
        self.assertIn('1962', message, 'disk-space refusal does not give the /swapfile size in MiB')
        self.assertIn('swapoff', message, 'disk-space refusal does not say how to free /swapfile')

    def test_2_refusal_without_swapfile_unchanged(self):
        message, backups = self.refusal(None)
        self.assertEqual(message, 'insufficient disk space before backup; services unchanged; ' + backups)
        self.assertNotIn('swapfile', message)


class PreflightAfterCleanInstallTests(unittest.TestCase):
    """Entry 2 GB RAM / 10 GB disk right after the first install, the next ./deploy.sh.

    Numbers of the node: 9978 MiB on the filesystem, 527 MiB of them the ext4
    reserve, 2000 MiB taken by the clean OS and 5556 MiB after the install.
    """
    USABLE, CLEAN, INSTALLED = (9978 - 527) * MIB, 2000 * MIB, 5556 * MIB
    INSTALL_SIZE = 6 * GIB  # base_swap_install_bytes of role base for the entry

    def swap_size(self, install_bytes):
        release = module('release')  # puts infra/vpn2/src on sys.path
        self.assertTrue(release)
        from vpn2 import swap
        size, _ = swap.plan(2014000, self.USABLE - self.CLEAN, 2 * GIB, swap.pending(install_bytes, self.CLEAN))
        return size

    def refusal(self, free):
        """Text of the preflight refusal with that much free space; '' - the deploy goes on."""
        state = module('deploy_state')
        release = module('release')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / 'os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
            (work / 'meminfo').write_text('MemAvailable: 4000000 kB\n')
            manifest = {'os': {'ubuntu': ['26.04']}, 'architectures': [release.architecture()],
                        'min_free_bytes': 2 * GIB, 'min_memory_bytes': 0, 'database': {},
                        'local_service_api': {'min': 0, 'max': 2 ** 31}}
            usage = patch.object(state.shutil, 'disk_usage', lambda _path: type('Usage', (), {'free': free}))
            with usage, fake_swapfile(None), patch.object(state, 'compatible', lambda *_args, **_kwargs: None):
                try:
                    state.preflight(manifest, work / 'db.sqlite', work, work / 'os-release', work / 'meminfo',
                                    warn=False)
                except state.ReleaseError as refused:
                    return str(refused)
        return ''

    def test_1_old_swap_size_is_refused_on_the_second_deploy(self):
        free = self.USABLE - self.INSTALLED - self.swap_size(0)
        self.assertIn('insufficient disk space before backup', self.refusal(free))

    def test_2_swap_sized_below_the_install_lets_the_second_deploy_through(self):
        size = self.swap_size(self.INSTALL_SIZE)
        self.assertEqual(size, 747 * MIB)
        self.assertEqual(self.refusal(self.USABLE - self.INSTALLED - size), '')

    def test_3_backups_may_grow_up_to_the_limit_and_not_a_byte_over(self):
        free = self.USABLE - self.INSTALLED - self.swap_size(self.INSTALL_SIZE)
        room = free - 2 * GIB
        self.assertGreaterEqual(room, 512 * MIB)
        self.assertEqual(self.refusal(free - room), '')
        self.assertIn('insufficient disk space before backup', self.refusal(free - room - 1))


if __name__ == '__main__':
    unittest.main()
