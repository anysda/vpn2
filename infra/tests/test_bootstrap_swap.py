"""Bootstrap turns on a swap file on hosts below 4 GiB that have no swap.

The step is the self-contained function ensure_swap in 00-bootstrap.sh. The
test cuts it out of the stage, points /proc/meminfo, /proc/swaps, /etc/fstab,
/etc/sysctl.d/ and /swapfile into a temporary tree and replaces fallocate,
mkswap, swapon, swapoff and sysctl with recorders in PATH.
"""
import os
from pathlib import Path
import re
import tempfile
import unittest

from test_deploy import bash, source


MIB = 1024 * 1024
GIB = 1024 * MIB

STUBS = {
    'fallocate': r'''
echo "fallocate $*" >> "$FIX/calls"
length= file=
while (( $# )); do
  case $1 in
    -l) length=$2; shift ;;
    --length=*) length=${1#--length=} ;;
    -*) ;;
    *) file=$1 ;;
  esac
  shift
done
truncate -s "$length" "$file"
''',
    'mkswap': 'echo "mkswap $*" >> "$FIX/calls"\n',
    'swapoff': 'echo "swapoff $*" >> "$FIX/calls"\n',
    'sysctl': 'echo "sysctl $*" >> "$FIX/calls"\n',
    'swapon': r'''
echo "swapon $*" >> "$FIX/calls"
for arg in "$@"; do
  case $arg in
    -s|--summary) cat "$FIX/proc/swaps"; exit 0 ;;
    --show*) tail -n +2 "$FIX/proc/swaps" | awk '{print $1}'; exit 0 ;;
  esac
done
for arg in "$@"; do
  [[ $arg == -* ]] && continue
  printf '%s file 0 0 -2\n' "$arg" >> "$FIX/proc/swaps"
done
''',
}

SWAPS_HEADER = 'Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n'


def ensure_swap_block():
    text = source('infra/scripts/00-bootstrap.sh')
    start = re.search(r'^ensure_swap\(\)\s*\{', text, re.M)
    if start is None:
        # The stage on HEAD has no swap step: bootstrap leaves memory as is.
        return 'ensure_swap() { :; }\n'
    return text[start.start():text.index('\n}', start.end()) + 3]


class Fixture:
    def __init__(self, directory, mem_mib, swaps=''):
        self.root = Path(directory)
        for sub in ('proc', 'etc/sysctl.d', 'bin'):
            (self.root / sub).mkdir(parents=True)
        (self.root / 'proc/meminfo').write_text(
            f'MemTotal:       {mem_mib * 1024} kB\n'
            f'MemFree:        {mem_mib * 512} kB\n'
            'SwapTotal:             0 kB\n')
        (self.root / 'proc/swaps').write_text(SWAPS_HEADER + swaps)
        (self.root / 'etc/fstab').write_text(
            'UUID=0a1b2c3d / ext4 defaults 0 1\n')
        for name, body in STUBS.items():
            stub = self.root / 'bin' / name
            stub.write_text('#!/usr/bin/env bash\nset -euo pipefail\n' + body)
            stub.chmod(0o755)
        self.swapfile = self.root / 'swapfile'

    def run(self):
        block = ensure_swap_block()
        for path in ('/proc/meminfo', '/proc/swaps', '/etc/fstab', '/etc/sysctl.d/'):
            block = block.replace(path, str(self.root) + path)
        block = block.replace('/swapfile', str(self.swapfile))
        script = (f'export FIX={self.root}\nexport PATH=$FIX/bin:$PATH\n'
                  'HOST_TAG=de\n' + block + 'ensure_swap\n')
        return bash(script, self.root)

    def calls(self, command=None):
        path = self.root / 'calls'
        lines = path.read_text().splitlines() if path.exists() else []
        return [line for line in lines if command is None or line.split()[0] == command]

    def fstab_swap_lines(self):
        return [line for line in (self.root / 'etc/fstab').read_text().splitlines()
                if line.split()[:1] == [str(self.swapfile)]]

    def swappiness_lines(self):
        found = []
        for conf in sorted((self.root / 'etc/sysctl.d').iterdir()):
            found += [line for line in conf.read_text().splitlines()
                      if re.match(r'\s*vm\.swappiness\s*=', line)]
        return found

    def snapshot(self):
        # system/ holds only links to the host PATH binaries: never walk into it.
        found = {}
        for folder, dirs, files in os.walk(self.root):
            folder = Path(folder)
            if folder == self.root:
                dirs[:] = [name for name in dirs if name != 'system']
            for name in files:
                path = folder / name
                if path.is_file() and folder.name != 'bin' and name != 'calls':
                    found[str(path.relative_to(self.root))] = path.read_bytes()
        return dict(sorted(found.items()))


class BootstrapSwapTests(unittest.TestCase):
    def run_ok(self, fixture):
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def assert_swap_created(self, fixture, size):
        self.assertTrue(fixture.swapfile.exists(), 'no swap file created')
        self.assertEqual(fixture.swapfile.stat().st_size, size)
        self.assertEqual(len(fixture.calls('fallocate')), 1, fixture.calls())
        self.assertIn(str(fixture.swapfile), ' '.join(fixture.calls('mkswap')))
        self.assertEqual([str(fixture.swapfile) in call for call in fixture.calls('swapon')],
                         [True], fixture.calls())
        lines = fixture.fstab_swap_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0].split()[2], 'swap', lines[0])
        self.assertEqual([re.sub(r'\s', '', line) for line in fixture.swappiness_lines()],
                         ['vm.swappiness=10'])

    def assert_untouched(self, fixture, before):
        self.assertFalse(fixture.swapfile.exists(), 'swap file created')
        self.assertEqual(fixture.snapshot(), before)
        for command in ('fallocate', 'mkswap', 'swapon', 'swapoff'):
            self.assertEqual(fixture.calls(command), [], command)
        self.assertEqual([call for call in fixture.calls('sysctl') if 'swappiness' in call], [])

    def test_bootstrap_calls_ensure_swap_for_every_role(self):
        text = source('infra/scripts/00-bootstrap.sh')
        self.assertTrue(re.search(r'(?m)^ensure_swap\(\)\s*\{', text), 'no ensure_swap step')
        self.assertTrue(re.search(r'(?m)^\s*ensure_swap\s*$', text), 'ensure_swap is never called')

    def test_2g_without_swap_creates_2g_swapfile(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, 2048)
            self.run_ok(fixture)
            self.assert_swap_created(fixture, 2 * GIB)

    def test_repeat_run_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, 2048)
            self.run_ok(fixture)
            self.run_ok(fixture)
            self.assert_swap_created(fixture, 2 * GIB)
            self.assertEqual(len(fixture.calls('mkswap')), 1, fixture.calls())

    def test_manual_swap_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, 2048, '/swap.img file 2097148 0 -2\n')
            (fixture.root / 'etc/fstab').write_text(
                'UUID=0a1b2c3d / ext4 defaults 0 1\n/swap.img none swap sw 0 0\n')
            (fixture.root / 'etc/sysctl.d/60-owner.conf').write_text('vm.swappiness = 60\n')
            before = fixture.snapshot()
            self.run_ok(fixture)
            self.assert_untouched(fixture, before)

    def test_4096_mib_gets_no_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, 4096)
            before = fixture.snapshot()
            self.run_ok(fixture)
            self.assert_untouched(fixture, before)

    def test_3839_mib_gets_2g_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, 3839)
            self.run_ok(fixture)
            self.assert_swap_created(fixture, 2 * GIB)

    def test_1g_gets_swapfile_of_ram_size(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, 1024)
            self.run_ok(fixture)
            self.assert_swap_created(fixture, GIB)

    def test_inactive_swapfile_from_broken_run_is_enabled_as_is(self):
        for in_fstab in (False, True):
            with self.subTest(in_fstab=in_fstab), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory, 2048)
                with fixture.swapfile.open('wb') as swapfile:
                    swapfile.truncate(GIB)
                if in_fstab:
                    with (fixture.root / 'etc/fstab').open('a') as fstab:
                        fstab.write(f'{fixture.swapfile} none swap sw 0 0\n')
                self.run_ok(fixture)
                self.assertEqual(fixture.calls('fallocate'), [], 'swap file recreated')
                self.assertEqual(fixture.swapfile.stat().st_size, GIB)
                self.assertEqual([str(fixture.swapfile) in call
                                  for call in fixture.calls('swapon')], [True], fixture.calls())
                self.assertEqual(len(fixture.fstab_swap_lines()), 1,
                                 fixture.fstab_swap_lines())

    def test_readme_says_deploy_adds_swap_below_4g(self):
        readme = source('README.md')
        self.assertFalse('Swap не нужен' in readme, 'README still says swap is not needed')
        self.assertTrue(any(re.search(r'(?i)swap', line) and re.search(r'4\s*(ГБ|GB)', line)
                            and 'депло' in line.lower() for line in readme.splitlines()),
                        'README does not say deploy adds swap below 4 GB')


if __name__ == '__main__':
    unittest.main()
