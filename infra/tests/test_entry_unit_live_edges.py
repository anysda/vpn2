"""Защита живых процессов entry: отказ записи виден, nice получают все потоки.

- запись oom_score_adj живому процессу не прошла - строка-предупреждение, rc 0;
- pid исчез между чтением cgroup.procs и записью - тихо, rc 0;
- многопоточный процесс фона: новый NI у каждого потока, tid берутся
  из cgroup.threads, без него - из /proc/<pid>/task.
"""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "infra/ansible/roles/resources/files/entry-resources.sh"
WARNING = "запись не прошла"
THREADED = ("import sys, threading, time\n"
            "for _ in range(3):\n"
            "    threading.Thread(target=time.sleep, args=(60,), daemon=True).start()\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n")


def dead_pid():
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


class LiveEdgeTests(unittest.TestCase):
    def spawn(self, argv):
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def threaded(self):
        process = self.spawn(["python3", "-c", THREADED])
        self.assertEqual(process.stdout.readline().strip(), "ready")
        tids = sorted(os.listdir(f"/proc/{process.pid}/task"), key=int)
        self.assertGreaterEqual(len(tids), 4)
        return process.pid, tids

    def run_stage(self, unit, root, procs, threads=None):
        cgroup = root / "cgroup/system.slice" / f"{unit}.service"
        cgroup.mkdir(parents=True, exist_ok=True)
        (cgroup / "cgroup.procs").write_text("".join(f"{pid}\n" for pid in procs))
        if threads is not None:
            (cgroup / "cgroup.threads").write_text("".join(f"{tid}\n" for tid in threads))
        command = (
            f'systemctl() {{ if [[ "$1" == show ]]; then echo /system.slice/{unit}.service; fi; }}; '
            f"source {shlex.quote(str(LIB))}; entry_unit_resources {unit}")
        env = dict(os.environ, ENTRY_UNIT_DIR=str(root / "units"),
                   ENTRY_CGROUP_ROOT=str(root / "cgroup"), ENTRY_PROC_ROOT=str(root / "proc"))
        result = subprocess.run(["bash", "-eu", "-c", command], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr.strip()[-300:])
        return result.stdout + result.stderr

    def fake_proc(self, root, pid, oom="0\n"):
        path = root / "proc" / str(pid)
        path.mkdir(parents=True, exist_ok=True)
        (path / "oom_score_adj").write_text(oom)
        return path

    def thread_nice(self, pid):
        out = subprocess.check_output(["ps", "-L", "-o", "tid=,ni=", "-p", str(pid)], text=True)
        return {tid: int(ni) for tid, ni in (line.split() for line in out.splitlines())}

    @unittest.skipIf(os.geteuid() == 0, "root пишет и в файл только для чтения")
    def test_readonly_oom_warns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sleeper = self.spawn(["sleep", "60"])
            target = self.fake_proc(root, sleeper.pid) / "oom_score_adj"
            target.chmod(0o444)
            output = self.run_stage("sing-box", root, [sleeper.pid])
            self.assertIn(f"oom_score_adj {sleeper.pid}: {WARNING}", output)
            self.assertEqual(target.read_text().strip(), "0")

    def test_vanished_pid_quiet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sleeper = self.spawn(["sleep", "60"])
            self.fake_proc(root, sleeper.pid)
            gone = dead_pid()
            output = self.run_stage("sing-box", root, [gone, sleeper.pid])
            self.assertNotIn(WARNING, output)
            self.assertEqual(output.strip(), "")
            oom = (root / "proc" / str(sleeper.pid) / "oom_score_adj").read_text().strip()
            self.assertEqual(oom, "-900")

    def test_vanished_pid_nice_quiet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_stage("anysda-yt-failover", root, [dead_pid()], [dead_pid()])
            self.assertEqual(output.strip(), "")

    def test_threads_from_cgroup_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid, tids = self.threaded()
            self.fake_proc(root, pid)
            self.run_stage("anysda-yt-failover", root, [pid], tids)
            nice = self.thread_nice(pid)
            self.assertEqual(sorted(nice, key=int), tids)
            self.assertEqual(set(nice.values()), {10}, nice)

    def test_threads_from_proc_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid, tids = self.threaded()
            for tid in tids:
                (self.fake_proc(root, pid) / "task" / tid).mkdir(parents=True)
            self.run_stage("anysda-yt-failover", root, [pid])
            nice = self.thread_nice(pid)
            self.assertEqual(sorted(nice, key=int), tids)
            self.assertEqual(set(nice.values()), {10}, nice)


if __name__ == "__main__":
    unittest.main()
