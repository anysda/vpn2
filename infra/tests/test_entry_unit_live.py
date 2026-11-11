"""Защита юнитов entry действует на уже стоящей машине, без перезапуска служб.

- system.slice получает MemoryLow не меньше суммы защищаемых юнитов ступени:
  с memory_recursiveprot защита ребёнка не выше защиты предков;
- повторный прогон даёт те же файлы и не зовёт daemon-reload;
- живому процессу защищаемого юнита ставится oom_score_adj -900, фону - nice 10,
  без restart.
"""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "infra/ansible/roles/resources/files/entry-resources.sh"
TOOL = ROOT / "infra/ansible/roles/resources/files/entry_resources.py"
DROPIN = "zz-vpn2-resources.conf"
PROTECTED = ("sing-box", "strongswan-starter", "openvpn-server@server")


def render(unit, ram):
    return subprocess.check_output([sys.executable, str(TOOL), "--ram-mib", str(ram),
                                    "--free-mib", "8192", "--unit", unit], text=True)


def memory_low(text):
    value = next(line.split("=", 1)[1] for line in text.splitlines()
                 if line.startswith("MemoryLow="))
    return int(value.removesuffix("M"))


class LiveProtectionTests(unittest.TestCase):
    def test_system_slice_covers_protected_units(self):
        for ram in (1024, 2048, 4096, 8192):
            with self.subTest(ram=ram):
                slice_text = render("system.slice", ram)
                self.assertTrue(slice_text.startswith("[Slice]\n"))
                total = sum(memory_low(render(unit, ram)) for unit in PROTECTED)
                self.assertGreaterEqual(memory_low(slice_text), total)

    def run_stage(self, unit, root):
        calls = root / "calls"
        units = root / "units"
        cgroup = root / "cgroup/system.slice" / f"{unit}.service"
        proc = root / "proc"
        sleeper = subprocess.Popen(["sleep", "60"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        cgroup.mkdir(parents=True, exist_ok=True)
        (cgroup / "cgroup.procs").write_text(f"{sleeper.pid}\n")
        (proc / str(sleeper.pid)).mkdir(parents=True, exist_ok=True)
        (proc / str(sleeper.pid) / "oom_score_adj").write_text("0\n")
        command = (
            f'systemctl() {{ echo "$*" >> {shlex.quote(str(calls))}; '
            f'if [[ "$1" == show ]]; then echo /system.slice/{unit}.service; fi; }}; '
            f"source {shlex.quote(str(LIB))}; entry_unit_resources {unit}")
        env = dict(os.environ, ENTRY_UNIT_DIR=str(units), ENTRY_CGROUP_ROOT=str(root / "cgroup"),
                   ENTRY_PROC_ROOT=str(proc))
        result = subprocess.run(["bash", "-eu", "-c", command], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr.strip()[-300:])
        return sleeper.pid, units, calls, proc

    def test_protected_unit_live_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid, units, calls, proc = self.run_stage("sing-box", root)
            slice_file = units / "system.slice.d" / DROPIN
            unit_file = units / "sing-box.service.d" / DROPIN
            self.assertTrue(slice_file.is_file(), "нет drop-in system.slice")
            self.assertGreaterEqual(memory_low(slice_file.read_text()),
                                    memory_low(unit_file.read_text()))
            self.assertEqual((proc / str(pid) / "oom_score_adj").read_text().strip(), "-900")
            first = calls.read_text().splitlines()
            self.assertEqual(first.count("daemon-reload"), 1)
            self.assertFalse([c for c in first if c.split()[0] in ("restart", "stop", "start",
                                                                   "try-restart", "reload")])
            files = {p: p.read_bytes() for p in (slice_file, unit_file)}

            pid, *_ = self.run_stage("sing-box", root)
            self.assertEqual({p: p.read_bytes() for p in files}, files)
            again = calls.read_text().splitlines()[len(first):]
            self.assertNotIn("daemon-reload", again)
            self.assertEqual((proc / str(pid) / "oom_score_adj").read_text().strip(), "-900")

    def test_background_unit_live_nice(self):
        with tempfile.TemporaryDirectory() as directory:
            pid, units, calls, _ = self.run_stage("anysda-yt-failover", Path(directory))
            self.assertEqual(os.getpriority(os.PRIO_PROCESS, pid), 10)
            self.assertFalse((units / "system.slice.d").exists())
            self.assertFalse([c for c in calls.read_text().splitlines()
                              if c.split()[0] in ("restart", "stop", "start")])


if __name__ == "__main__":
    unittest.main()
