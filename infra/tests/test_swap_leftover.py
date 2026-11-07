"""A swap file left by older deploys must not block the next one forever.

Entry 2 GB RAM / 10 GB disk: the old bootstrap made a 1962 MiB /swapfile, 1411
MiB stayed free and preflight stopped the deploy naming only the deploy
backups. A /swapfile with holes that swapon refuses stayed on disk and every
bootstrap printed "swap пропущен".

Preflight runs as in test_release with a min_free_bytes no disk has; the
/swapfile it may look at is answered by a stat stub. Bootstrap runs ensure_swap
on the fixture of test_bootstrap_swap_disk with a swapon that refuses a file
dd has not written.
"""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from test_bootstrap_swap import GIB, MIB
from test_bootstrap_swap_disk import DiskFixture
from test_release import module


SWAPFILE = '/swapfile'

# swapon on a file with holes: the kernel refuses it, mkswap does not help.
SWAPON = r'''
echo "swapon $*" >> "$FIX/calls"
for arg in "$@"; do
  case $arg in
    -s|--summary) cat "$FIX/proc/swaps"; exit 0 ;;
    --show*) tail -n +2 "$FIX/proc/swaps" | awk '{print $1}'; exit 0 ;;
  esac
done
for arg in "$@"; do
  [[ $arg == -* ]] && continue
  if [[ $arg == "$FIX/swapfile" && ! -f $FIX/dd_written ]]; then
    echo "swapon: $arg: skipping - it appears to have holes." >&2
    exit 255
  fi
  printf '%s file 0 0 -2\n' "$arg" >> "$FIX/proc/swaps"
done
'''

DD = r'''
echo "dd $*" >> "$FIX/calls"
bs= count= out=
for arg in "$@"; do
  case $arg in
    bs=*) bs=${arg#bs=} ;;
    count=*) count=${arg#count=} ;;
    of=*) out=${arg#of=} ;;
  esac
done
case $bs in
  *M) bs=$(( ${bs%M} * 1024 * 1024 )) ;;
  *K|*k) bs=$(( ${bs%[Kk]} * 1024 )) ;;
esac
truncate -s $(( bs * count )) "$out"
touch "$FIX/dd_written"
'''


class LeftoverFixture(DiskFixture):
    def __init__(self, directory, avail=20 * GIB):
        super().__init__(directory, 2048, avail)
        for name, body in (('swapon', SWAPON), ('dd', DD)):
            stub = self.root / 'bin' / name
            stub.write_text('#!/usr/bin/env bash\nset -euo pipefail\n' + body)
            stub.chmod(0o755)

    def run(self):
        with patch.dict(os.environ, {'VPN2_RELEASE_MANIFEST': str(self.root / 'no-manifest.json')}):
            return super().run()

    def active(self):
        return [line.split()[0] for line in
                (self.root / 'proc/swaps').read_text().splitlines()[1:] if line.strip()]


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


class BootstrapSwapLeftoverTests(unittest.TestCase):
    def run_ok(self, fixture):
        result = fixture.run()
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        return output

    def holey_fixture(self, directory, size=GIB):
        fixture = LeftoverFixture(directory)
        with fixture.swapfile.open('wb') as swapfile:
            swapfile.truncate(size)
        return fixture

    def assert_recreated(self, fixture, output):
        detail = f'{output}\n' + '\n'.join(fixture.calls())
        self.assertTrue(fixture.swapfile.exists(), 'swap file removed and not recreated\n' + detail)
        dd = fixture.calls('dd')
        self.assertEqual(len(dd), 1, 'holey /swapfile not rewritten with dd\n' + detail)
        self.assertIn(f'of={fixture.swapfile}', dd[0])
        self.assertEqual(fixture.calls('fallocate'), [], 'recreated with fallocate, not dd')
        self.assertEqual(fixture.swapfile.stat().st_size, 2 * GIB, detail)
        self.assertIn(str(fixture.swapfile), ' '.join(fixture.calls('mkswap')), detail)
        self.assertEqual(fixture.active(), [str(fixture.swapfile)], 'swap not turned on\n' + detail)
        self.assertEqual(len(fixture.fstab_swap_lines()), 1, fixture.fstab_swap_lines())

    def test_3_holey_swapfile_recreated_with_dd(self):
        for in_fstab in (False, True):
            with self.subTest(in_fstab=in_fstab), tempfile.TemporaryDirectory() as directory:
                fixture = self.holey_fixture(directory)
                if in_fstab:
                    with (fixture.root / 'etc/fstab').open('a') as fstab:
                        fstab.write(f'{fixture.swapfile} none swap sw 0 0\n')
                output = self.run_ok(fixture)
                print('bootstrap on holey /swapfile: ' + ' | '.join(output.splitlines()))
                self.assert_recreated(fixture, output)

    def test_4_active_swapfile_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.holey_fixture(directory, MIB)
            (fixture.root / 'proc/swaps').write_text(
                (fixture.root / 'proc/swaps').read_text() + f'{fixture.swapfile} file 1020 0 -2\n')
            with (fixture.root / 'etc/fstab').open('a') as fstab:
                fstab.write(f'{fixture.swapfile} none swap sw 0 0\n')
            before = fixture.snapshot()
            self.run_ok(fixture)
            for command in ('dd', 'fallocate', 'mkswap', 'swapon', 'swapoff'):
                self.assertEqual(fixture.calls(command), [], command)
            self.assertEqual(fixture.snapshot(), before)

    def test_5_repeat_after_recreate_keeps_file(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.holey_fixture(directory)
            first = self.run_ok(fixture)
            self.assert_recreated(fixture, first)
            calls = len(fixture.calls())
            second = self.run_ok(fixture)
            self.assertEqual(fixture.calls()[calls:], [], 'second bootstrap touched swap again\n' + second)
            self.assert_recreated(fixture, first + second)


if __name__ == '__main__':
    unittest.main()
