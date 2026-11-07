"""Bootstrap leaves swap alone inside a container.

In a privileged LXC swapon on the guest /swapfile succeeds and puts a file of
the container file system into the host swap: rollback and destroy of the
container then fail. ensure_swap asks systemd-detect-virt --container and
skips swap there; a VM, bare metal or a VPS without systemd-detect-virt keep
the old behaviour.
"""
import os
from pathlib import Path
import tempfile
import unittest

import test_bootstrap_swap as base


def detect_virt_stub(output, code):
    return (f'echo "systemd-detect-virt $*" >> "$FIX/calls"\n'
            f'[[ "$*" == *--container* ]] || exit 1\n'
            f'echo {output}\nexit {code}\n')


class VirtFixture(base.Fixture):
    """Fixture with systemd-detect-virt stubbed or missing from PATH."""

    def __init__(self, directory, mem_mib, virt):
        super().__init__(directory, mem_mib)
        self.system = self.root / 'system'
        self.system.mkdir()
        seen = set()
        # Everything the stage needs from the system PATH, minus the real
        # systemd-detect-virt: the test must not depend on where it runs.
        for folder in os.environ.get('PATH', '').split(os.pathsep):
            if not folder or not Path(folder).is_dir():
                continue
            for entry in Path(folder).iterdir():
                if entry.name in seen or entry.name == 'systemd-detect-virt':
                    continue
                if entry.is_file() and os.access(entry, os.X_OK):
                    seen.add(entry.name)
                    (self.system / entry.name).symlink_to(entry)
        dd = self.root / 'bin/dd'
        dd.write_text('#!/usr/bin/env bash\necho "dd $*" >> "$FIX/calls"\n'
                      f'exec {self.system / "dd"} "$@"\n')
        dd.chmod(0o755)
        if virt is not None:
            stub = self.root / 'bin/systemd-detect-virt'
            stub.write_text('#!/usr/bin/env bash\n' + detect_virt_stub(*virt))
            stub.chmod(0o755)

    def run(self):
        saved = os.environ['PATH']
        os.environ['PATH'] = str(self.system)
        try:
            return super().run()
        finally:
            os.environ['PATH'] = saved


class BootstrapSwapContainerTests(unittest.TestCase):
    run_ok = base.BootstrapSwapTests.run_ok
    assert_swap_created = base.BootstrapSwapTests.assert_swap_created
    assert_untouched = base.BootstrapSwapTests.assert_untouched

    def test_lxc_gets_no_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = VirtFixture(directory, 2048, ('lxc', 0))
            before = fixture.snapshot()
            result = self.run_ok(fixture)
            self.assert_untouched(fixture, before)
            self.assertIn('swap пропущен: контейнер', result.stdout)
            for command in ('dd', 'mkswap', 'swapon', 'fallocate'):
                self.assertEqual(fixture.calls(command), [], command)

    def test_vm_without_container_keeps_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = VirtFixture(directory, 2048, ('none', 1))
            result = self.run_ok(fixture)
            self.assert_swap_created(fixture, 2 * base.GIB)
            self.assertNotIn('контейнер', result.stdout)

    def test_vps_without_detect_virt_keeps_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = VirtFixture(directory, 2048, None)
            result = self.run_ok(fixture)
            self.assert_swap_created(fixture, 2 * base.GIB)
            self.assertNotIn('контейнер', result.stdout)


if __name__ == '__main__':
    unittest.main()
