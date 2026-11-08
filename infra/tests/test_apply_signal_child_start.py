"""A signal while apply starts a child stops apply with its code and leaves no child running.

The signal comes when the child already runs and Popen has not returned it yet: apply sends it to
itself right there, so no timing decides the case. The child hangs and is deaf to SIGTERM or not.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

import test_apply_openvpn_candidate as candidate
from test_apply_openvpn_check_env import script

TESTS = Path(__file__).resolve().parent
SIGNALS = ('SIGTERM', 'SIGHUP', 'SIGINT')
# --server is the cipher check's own start, --genkey one through run().
WORDS = ('--server', '--genkey')
LIMIT = 30
GONE = 5
RUNNER = '''
import os, signal, subprocess, sys, tempfile
from pathlib import Path
tests, root, state, new, scratch, stub, word, name, pid = sys.argv[1:]
sys.path.insert(0, tests)
from test_apply import Host, apply
tempfile.tempdir = scratch
real = subprocess.Popen
class Popen(real):
    def __init__(self, argv, *args, **kwargs):
        super().__init__([stub if each == '/usr/sbin/openvpn' else each for each in argv], *args, **kwargs)
        if word in argv:
            Path(pid).write_text(str(self.pid))
            os.kill(os.getpid(), getattr(signal, name))
subprocess.Popen = Popen
applier = apply.Applier(Path(root), Path(state), Host(Path(root)))
applier.settle = 0
applier.install('openvpn', new)
print('installed', flush=True)
'''


@unittest.skipUnless(Path(candidate.OPENVPN).exists(), 'needs ' + candidate.OPENVPN)
class SignalWhileChildStartsTests(unittest.TestCase):
    setUp_tree = candidate.OpenvpnCandidateTests.setUp

    def setUp(self):
        self.setUp_tree()
        self.scratch = self.work / 'scratch'
        self.scratch.mkdir()
        self.new.write_text(candidate.WORKING + 'cipher BF-CBC\n')
        self.child_pid = self.work / 'child.pid'
        self.addCleanup(subprocess.run, ['pkill', '-KILL', '-f', f'[/]{str(self.work)[1:]}'], capture_output=True)

    def stub(self, word, deaf):
        """openvpn that hangs on a call with WORD; any other call is the real one. apply's Popen gives its pid."""
        trap = "trap '' TERM; " if deaf else ''
        return script(self.work / 'openvpn', f'for each; do [ "$each" = "{word}" ] && {{ {trap}exec sleep 1000; }}; done\n'
                                             f'exec {candidate.OPENVPN} "$@"')

    def running(self, pid):
        """PID still runs GONE s after apply exits; a zombie waiting for its parent does not run."""
        stat = Path('/proc', pid, 'stat')
        started = time.monotonic()
        while time.monotonic() - started < GONE:
            try:
                if stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                    return False
            except FileNotFoundError:
                return False
            time.sleep(0.05)
        return True

    def stopped(self, word, name, deaf):
        self.child_pid.unlink(missing_ok=True)
        try:
            run = subprocess.run([sys.executable, '-B', '-I', '-c', RUNNER, str(TESTS), str(self.root), str(self.state),
                                  str(self.new), str(self.scratch), self.stub(word, deaf), word, name, str(self.child_pid)],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=LIMIT,
                                 start_new_session=True)
        except subprocess.TimeoutExpired as error:
            self.fail(f'apply still runs {LIMIT} s on: {error.output}')
        self.assertTrue(self.child_pid.exists(), f'apply did not start the child: {run.stdout}')
        child = self.child_pid.read_text().strip()
        self.assertEqual((run.returncode, run.stdout, self.running(child), sorted(os.listdir(self.scratch)),
                          (self.base / 'server.conf').read_text() == candidate.WORKING),
                         (128 + getattr(signal, name), '', False, [], True))

    def test_signal_while_a_child_starts_leaves_no_child(self):
        for word in WORDS:
            for deaf in (False, True):
                for name in SIGNALS:
                    with self.subTest(child=word, signal=name, deaf_to_sigterm=deaf):
                        self.stopped(word, name, deaf)


if __name__ == '__main__':
    unittest.main()
