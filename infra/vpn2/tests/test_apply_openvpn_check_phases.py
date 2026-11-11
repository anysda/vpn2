"""OpenVPN apply stopped by a signal in any phase exits 128+sig and leaves no candidate, openvpn or temporary file."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

import test_apply_openvpn_candidate as candidate
import test_apply_openvpn_check_env as env
from test_apply_openvpn_check_env import script
from test_apply_openvpn_check_signal import TESTS, Stopped

OPENVPN = candidate.OPENVPN
WORKING = candidate.WORKING
GOOD = env.GOOD
BROKEN = WORKING + '# BROKEN\n'
# apply install or rollback in a process of its own. A phase is held by an openvpn call
# with this word (the stub sleeps) or by the first file apply writes in this directory.
RUNNER = '''
import os, subprocess, sys, tempfile, time
from pathlib import Path
tests, root, state, scratch, stub, hold, held, action, new = sys.argv[1:]
sys.path.insert(0, tests)
from test_apply import Host, apply
tempfile.tempdir = scratch
real = subprocess.Popen
def popen(argv, *args, **kwargs):
    return real([stub if word == '/usr/sbin/openvpn' else word for word in argv], *args, **kwargs)
subprocess.Popen = popen
fsync = os.fsync
def held_fsync(descriptor):
    if Path(os.readlink(f'/proc/self/fd/{descriptor}')).parent == Path(hold):
        Path(held).write_text(str(os.getpid()))
        time.sleep(1000)
    fsync(descriptor)
os.fsync = held_fsync
applier = apply.Applier(Path(root), Path(state), Host(Path(root)))
applier.settle = 0
try:
    if action == 'install':
        applier.install('openvpn', new)
    else:
        applier.rollback('openvpn')
except apply.Failed as error:
    print('failed:', error, flush=True)
    sys.exit(error.code)
print('done', flush=True)
'''
SIGNALS = ('SIGTERM', 'SIGHUP', 'SIGINT')
LIMIT = 30


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnSecondSignalTests(Stopped, unittest.TestCase):
    setUp_tree = candidate.OpenvpnCandidateTests.setUp

    def test_second_signal_while_apply_stops_leaves_nothing(self):
        # The first signal decides the code; one that comes while apply cleans up does not cut it short.
        self.each([], [({'signals': f'{first}+{second}', 'deaf_to_sigterm': deaf},
                        ([(2, first), (0.05, second)], 'kill', deaf))
                       for deaf in (False, True)
                       for first, second in (('SIGINT', 'SIGINT'), ('SIGTERM', 'SIGINT'),
                                             ('SIGHUP', 'SIGINT'), ('SIGTERM', 'SIGTERM'))])


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnApplyPhaseSignalTests(unittest.TestCase):
    """TERM, HUP or INT in each phase of install and restore, the cipher check and around it."""
    setUp_tree = candidate.OpenvpnCandidateTests.setUp
    maxDiff = None

    def setUp(self):
        self.setUp_tree()
        self.scratch = self.work / 'scratch'
        self.scratch.mkdir()
        self.held = self.work / 'held.pid'
        self.runner = self.work / 'runner.py'
        self.runner.write_text(RUNNER)
        self.addCleanup(self.sweep)

    def stub(self, word):
        """openvpn that sleeps on a call with WORD, its pid in held.pid; any other call is the real one."""
        return script(self.work / 'openvpn', f'for word; do [ "$word" = "{word}" ] && '
                                             f'{{ echo $$ > {self.held}; exec sleep 1000; }}; done\n'
                                             f'exec {OPENVPN} "$@"')

    def sweep(self):
        # [/]tmp/... finds the processes under the work directory, not this pkill; the stub
        # that held a phase is a sleep by then, found by its pid.
        subprocess.run(['pkill', '-KILL', '-f', f'[/]{str(self.work)[1:]}'], capture_output=True)
        if self.held.exists():
            subprocess.run(['kill', '-KILL', self.held.read_text().strip()], capture_output=True)
        for path in [*self.scratch.iterdir(), *self.state.glob('.vpn2-apply-*'), self.held,
                     self.applier.journal('openvpn'),
                     *(path for path in self.base.iterdir() if path.name not in ('ccd', 'pki', 'server.conf'))]:
            path.unlink(missing_ok=True)

    def stopped(self, action, phase, hold, name):
        """apply ACTION held in PHASE by HOLD (an openvpn word or a directory), stopped by signal NAME."""
        number = getattr(signal, name)
        working = WORKING if action == 'install' else BROKEN
        if action == 'restore':
            (self.base / 'server.conf').write_text(GOOD)
            self.applier.mark('openvpn')
        (self.base / 'server.conf').write_text(working)
        self.new.write_text(GOOD)
        good = self.applier.good('openvpn').read_text()
        word = hold if isinstance(hold, str) else '--no-such-word'
        directory = str(hold) if isinstance(hold, Path) else '/nonexistent'
        check = subprocess.Popen([sys.executable, '-B', '-I', str(self.runner), str(TESTS), str(self.root),
                                  str(self.state), str(self.scratch), self.stub(word), directory, str(self.held),
                                  action, str(self.new)],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            started = time.monotonic()
            while not self.held.exists() and time.monotonic() - started < 20 and check.poll() is None:
                time.sleep(0.05)
            self.assertTrue(self.held.exists(), f'apply did not reach {phase}: {check.poll()}')
            time.sleep(0.3)
            os.kill(check.pid, number)
            said = check.communicate(timeout=LIMIT)[0]
        except subprocess.TimeoutExpired:
            check.kill()
            self.fail(f'apply still runs {LIMIT} s on: {check.communicate()[0]}')
        time.sleep(0.5)
        holder = self.held.read_text().strip()
        left = subprocess.run(['pgrep', '-a', '-f', f'[/]{str(self.work)[1:]}'], capture_output=True, text=True)
        # All at once: a stop by the default handler shows each trace it leaves.
        self.assertEqual({'code': check.returncode, 'done': 'done' in said,
                          'holder runs': Path('/proc', holder).exists(), 'processes': left.stdout.splitlines(),
                          'temporary directory': sorted(path.name for path in self.scratch.iterdir()),
                          'state': sorted(path.name for path in self.state.glob('.vpn2-apply-*')),
                          'openvpn directory': sorted(path.name for path in self.base.iterdir()),
                          'working config': (self.base / 'server.conf').read_text() == working,
                          'last good': self.applier.good('openvpn').read_text() == good},
                         {'code': 128 + number, 'done': False, 'holder runs': False, 'processes': [],
                          'temporary directory': [], 'state': [], 'openvpn directory': ['ccd', 'pki', 'server.conf'],
                          'working config': True, 'last good': True}, said)

    def each(self, action, phases):
        for phase, hold in phases:
            for name in SIGNALS:
                with self.subTest(action=action, phase=phase, signal=name):
                    try:
                        self.stopped(action, phase, hold, name)
                    finally:
                        self.sweep()

    def test_signal_in_each_phase_of_install_leaves_nothing(self):
        self.each('install', (('writing the candidate', self.base),
                              ('--show-ciphers hangs', '--show-ciphers'),
                              ('--genkey hangs', '--genkey'),
                              ('cipher check', '--server'),
                              # The journal says "replacing" only once written; until then the candidate goes.
                              ('after the check, before the working config', self.state)))

    def test_signal_in_each_phase_of_restore_leaves_nothing(self):
        # restore checks last good where it lies and writes no candidate: its write is the working config's.
        self.each('restore', (('--show-ciphers hangs', '--show-ciphers'),
                              ('--genkey hangs', '--genkey'),
                              ('cipher check', '--server'),
                              ('after the check, writing the working config', self.base)))


if __name__ == '__main__':
    unittest.main()
