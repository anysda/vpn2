"""A signal after the one that stopped apply keeps the code of the first, up to the last step of the process.

apply sends each later signal to itself at a step it names, so no timing decides the case: in the
cleanup, after it, in atexit, and while the interpreter shuts down, where Python puts back SIG_DFL
for each signal it had a handler for.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

import test_apply_openvpn_candidate as candidate
from test_apply_openvpn_check_env import hanging

TESTS = Path(__file__).resolve().parent
SIGNALS = ('SIGTERM', 'SIGHUP', 'SIGINT')
STEPS = ('cleanup', 'after cleanup', 'atexit', 'shutdown')
LIMIT = 30
# The later signals apply sends itself at STEP; "shutdown" is a module global that goes
# while the interpreter shuts down.
LATE = '''
import atexit, os, signal
def late(step, names):
    def send():
        for name in names:
            os.kill(os.getpid(), getattr(signal, name))
    if step == 'atexit':
        atexit.register(send)
    elif step == 'shutdown':
        class Shutdown:
            def __del__(self):
                send()
        return Shutdown()
    return send
'''
STOPPING = LATE + '''
import sys
tests, first, step, *names = sys.argv[1:]
sys.path.insert(0, tests)
from test_apply import apply
send = late(step, names)
try:
    with apply.stopping():
        try:
            os.kill(os.getpid(), getattr(signal, first))
            while True:
                pass
        finally:
            if step == 'cleanup':
                send()
finally:
    if step == 'after cleanup':
        send()
'''
INSTALL = LATE + '''
import subprocess, sys, tempfile
from pathlib import Path
tests, root, state, new, scratch, stub, pid, *names = sys.argv[1:]
sys.path.insert(0, tests)
from test_apply import Host, apply
tempfile.tempdir = scratch
real = subprocess.Popen
def popen(argv, *args, **kwargs):
    return real([stub if word == '/usr/sbin/openvpn' else word for word in argv], *args, **kwargs)
subprocess.Popen = popen
send = late('shutdown', names)
applier = apply.Applier(Path(root), Path(state), Host(Path(root)))
applier.settle = 0
Path(pid).write_text(str(os.getpid()))
applier.install('openvpn', new)
print('installed', flush=True)
'''


class SignalAfterStopTests(unittest.TestCase):
    def stopped(self, first, step, later):
        run = subprocess.run([sys.executable, '-B', '-I', '-c', STOPPING, str(TESTS), first, step, *later],
                             capture_output=True, text=True, timeout=LIMIT)
        self.assertEqual((run.returncode, run.stdout + run.stderr), (128 + getattr(signal, first), ''))

    def test_later_signal_at_each_step_keeps_the_first_code(self):
        for first in SIGNALS:
            for step in STEPS:
                for second in SIGNALS:
                    with self.subTest(first=first, step=step, second=second):
                        self.stopped(first, step, [second])

    def test_third_signal_keeps_the_first_code(self):
        for first in SIGNALS:
            for step in STEPS:
                with self.subTest(first=first, step=step):
                    self.stopped(first, step, [name for name in SIGNALS if name != first])

    def test_one_signal_exits_with_its_code(self):
        for first in SIGNALS:
            with self.subTest(first=first):
                self.stopped(first, 'none', [])


@unittest.skipUnless(Path(candidate.OPENVPN).exists(), 'needs ' + candidate.OPENVPN)
class InstallSignalAfterStopTests(unittest.TestCase):
    """install stopped in the cipher check, its server deaf to SIGTERM or not, a later signal at shutdown."""
    setUp_tree = candidate.OpenvpnCandidateTests.setUp

    def setUp(self):
        self.setUp_tree()
        self.scratch = self.work / 'scratch'
        self.scratch.mkdir()
        self.new.write_text(candidate.WORKING + 'cipher BF-CBC\n')
        self.addCleanup(subprocess.run, ['pkill', '-KILL', '-f', f'[/]{str(self.work)[1:]}'], capture_output=True)

    def stopped(self, first, later, deaf):
        apply_pid, server_pid = self.work / 'apply.pid', self.work / 'server.pid'
        for path in (apply_pid, server_pid):
            path.unlink(missing_ok=True)
        check = subprocess.Popen([sys.executable, '-B', '-I', '-c', INSTALL, str(TESTS), str(self.root),
                                  str(self.state), str(self.new), str(self.scratch), hanging(self.work, deaf),
                                  str(apply_pid), *later],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            started = time.monotonic()
            while not server_pid.exists() and time.monotonic() - started < 20 and check.poll() is None:
                time.sleep(0.05)
            self.assertTrue(server_pid.exists(), 'the check did not start the server')
            os.kill(int(apply_pid.read_text()), getattr(signal, first))
            said = check.communicate(timeout=LIMIT)[0]
        except subprocess.TimeoutExpired:
            check.kill()
            self.fail(f'apply still runs {LIMIT} s on: {check.communicate()[0]}')
        server = server_pid.read_text().strip()
        self.assertEqual((check.returncode, said, Path('/proc', server).exists(), sorted(os.listdir(self.scratch)),
                          (self.base / 'server.conf').read_text() == candidate.WORKING),
                         (128 + getattr(signal, first), '', False, [], True))

    def test_later_signal_at_shutdown_keeps_the_first_code(self):
        for deaf in (False, True):
            for first in SIGNALS:
                for later in [[name] for name in SIGNALS if name != first] + [list(SIGNALS)] + [[]]:
                    with self.subTest(first=first, later='+'.join(later), deaf_to_sigterm=deaf):
                        self.stopped(first, later, deaf)


if __name__ == '__main__':
    unittest.main()
