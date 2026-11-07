"""The swap file bootstrap creates must leave the disk space deploy preflight wants.

Stand, entry 2 GB RAM / 10 GB disk: the first deploy made a 1962 MiB /swapfile,
1411 MiB stayed free and the next deploy stopped in preflight on
min_free_bytes (2 GiB). The test runs ensure_swap from 00-bootstrap.sh on the
fixture of test_bootstrap_swap and adds a df that reports the free space minus
the swap file it made.
"""
import re
import tempfile
import unittest

from test_bootstrap_swap import GIB, MIB, Fixture
from test_deploy import source


# min_free_bytes of the release manifest, infra/lib/build-release.py; preflight
# in infra/lib/deploy_state.py stops a deploy below it.
MIN_FREE = int(re.search(r"'min_free_bytes':\s*(\d+)",
                         source('infra/lib/build-release.py')).group(1))
MARGIN = 512 * MIB
MIN_SWAP = 256 * MIB

DF = r'''
avail=$(cat "$FIX/df_avail")
[[ -f $FIX/swapfile ]] && avail=$(( avail - $(stat -c %s "$FIX/swapfile") ))
printf 'Filesystem 1-blocks Used Available Capacity Mounted\n'
printf '/dev/vda1 10737418240 0 %s 0%% /\n' "$avail"
'''


class DiskFixture(Fixture):
    def __init__(self, directory, mem_mib, avail):
        super().__init__(directory, mem_mib)
        (self.root / 'df_avail').write_text(str(avail))
        stub = self.root / 'bin/df'
        stub.write_text('#!/usr/bin/env bash\nset -euo pipefail\n' + DF)
        stub.chmod(0o755)
        self.avail = avail

    def free_after(self):
        size = self.swapfile.stat().st_size if self.swapfile.exists() else 0
        return self.avail - size


class BootstrapSwapDiskTests(unittest.TestCase):
    def run_ok(self, fixture):
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_manifest_min_free_is_2g(self):
        # ensure_swap has no way to get the manifest threshold, so the reserve it
        # keeps must follow this number in infra/lib/build-release.py.
        self.assertEqual(MIN_FREE, 2147483648)

    def test_2g_ram_3_4g_free_leaves_preflight_space(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = DiskFixture(directory, 2048, 3482 * MIB)
            output = self.run_ok(fixture)
            free = fixture.free_after()
            self.assertGreaterEqual(
                free, MIN_FREE + MARGIN,
                f'free after swap {free // MIB} MiB < min_free_bytes '
                f'{MIN_FREE // MIB} + {MARGIN // MIB} MiB\n{output}')
            if fixture.swapfile.exists():
                self.assertGreaterEqual(fixture.swapfile.stat().st_size, MIN_SWAP)
            self.assertTrue(
                any('swap' in line and 'свободно' in line for line in output.splitlines()),
                output)

    def test_too_little_free_for_256m_swap_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = DiskFixture(directory, 2048, MIN_FREE + MARGIN + 128 * MIB)
            output = self.run_ok(fixture)
            self.assertFalse(fixture.swapfile.exists(), output)
            self.assertEqual(fixture.calls('fallocate'), [])
            self.assertEqual(fixture.calls('swapon'), [])
            self.assertRegex(output, r'swap пропущен')

    def test_20g_free_keeps_ram_sized_swap(self):
        for mem_mib, size in ((2048, 2 * GIB), (1024, GIB), (3839, 2 * GIB)):
            with self.subTest(mem_mib=mem_mib), tempfile.TemporaryDirectory() as directory:
                fixture = DiskFixture(directory, mem_mib, 20 * GIB)
                output = self.run_ok(fixture)
                self.assertTrue(fixture.swapfile.exists(), output)
                self.assertEqual(fixture.swapfile.stat().st_size, size)


if __name__ == '__main__':
    unittest.main()
