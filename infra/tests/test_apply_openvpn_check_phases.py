"""OpenVPN cipher check: a signal in any phase leaves no server, no key and no candidate; last good comes back
where the check cannot start."""
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from test_apply import apply
import test_apply_openvpn_candidate as candidate
import test_apply_openvpn_check_env as env
from test_apply_openvpn_check_env import script

OPENVPN = candidate.OPENVPN
TESTS = Path(__file__).resolve().parent
WORKING = candidate.WORKING
KNOWN = env.KNOWN
# apply installs a candidate in a process of its own; HOOK picks the phase the signal comes in.
# timer: the server has started, the check has not set its deadline yet.
# rmtree: the check is over and its scratch with the keys is being removed.
RUNNER = '''
import os, shutil, signal, subprocess, sys, tempfile, threading, time
from pathlib import Path
tests, root, state, new, scratch, stub, pid, stub_pid, hook, name = sys.argv[1:]
sys.path.insert(0, tests)
from test_apply import Host, apply
tempfile.tempdir = scratch
fired = []
def signal_self():
    if fired:
        return
    fired.append(1)
    print('hook', hook, flush=True)
    os.kill(os.getpid(), getattr(signal, name))
    time.sleep(0.5)  # the handler runs here, inside the phase
real_popen = subprocess.Popen
def popen(argv, *args, **kwargs):
    if '/usr/bin/unshare' in argv:
        argv = [stub if word == '/usr/sbin/openvpn' else word for word in argv]
    return real_popen(argv, *args, **kwargs)
subprocess.Popen = popen
if hook == 'timer':
    real_timer = threading.Timer
    def timer(*args, **kwargs):
        started = time.monotonic()
        while not os.path.exists(stub_pid) and time.monotonic() - started < 20:
            time.sleep(0.05)
        signal_self()
        return real_timer(*args, **kwargs)
    threading.Timer = timer
if hook == 'rmtree':
    real_rmtree = shutil.rmtree
    def rmtree(path, *args, **kwargs):
        if str(path).startswith(scratch):
            signal_self()
        return real_rmtree(path, *args, **kwargs)
    shutil.rmtree = rmtree
Path(pid).write_text(str(os.getpid()))
applier = apply.Applier(Path(root), Path(state), Host(Path(root)))
applier.settle = 0
try:
    applier.install('openvpn', new)
except apply.Failed as error:
    print('failed:', error, flush=True)
    sys.exit(error.code)
print('installed', flush=True)
'''
# A server deaf to SIGTERM is killed 10 s on.
LIMIT = 10 + 20


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
@candidate.needs_server
class OpenvpnCheckPhasesTests(unittest.TestCase):
    setUp_tree = candidate.OpenvpnCandidateTests.setUp

    def setUp(self):
        self.setUp_tree()
        self.addCleanup(self.sweep)
        self.scratch = self.work / 'scratch'
        self.scratch.mkdir()
        self.apply_pid, self.stub_pid = self.work / 'apply.pid', self.work / 'stub.pid'
        self.new.write_text(WORKING + 'cipher BF-CBC\n')
        # Root's leftovers must not keep the tree from going.
        self.addCleanup(subprocess.run, ['sudo', '-n', 'chmod', '-R', 'a+rwX', str(self.work)], capture_output=True)

    def sweep(self):
        """Whatever a check under test left: processes of this tree, root's scratch and the candidate."""
        # [/]tmp/... finds the processes under the work directory, not this pkill.
        subprocess.run(candidate.AS_ROOT + ['pkill', '-KILL', '-f', f'[/]{str(self.work)[1:]}'], capture_output=True)
        if self.stub_pid.exists():
            subprocess.run(candidate.AS_ROOT + ['kill', '-KILL', self.stub_pid.read_text().strip()],
                           capture_output=True)
        subprocess.run(candidate.AS_ROOT + ['find', str(self.scratch), '-mindepth', '1', '-delete'],
                       capture_output=True)
        subprocess.run(candidate.AS_ROOT + ['rm', '-f', str(self.applier.candidate('openvpn'))], capture_output=True)
        for path in (self.apply_pid, self.stub_pid):
            path.unlink(missing_ok=True)

    def stub(self, deaf=False, starts=False):
        """A server that says it started or never does; deaf, it does not exit on SIGTERM either."""
        trap = "trap '' TERM\n" if deaf else ''
        said = 'echo "Initialization Sequence Completed"\n' if starts else ''
        return script(self.work / 'openvpn', f'echo $$ > {self.stub_pid}\n{trap}{said}exec sleep 1000')

    def stopped(self, prefix, stub, hook='none', name='SIGTERM', signals=()):
        """apply stopped by a hook in a phase or by SIGNALS from outside, (delay, name) each."""
        runner = self.work / 'runner.py'
        runner.write_text(RUNNER)
        argv = prefix + [sys.executable, '-B', '-I', str(runner), str(TESTS), str(self.root), str(self.state),
                         str(self.new), str(self.scratch), stub, str(self.apply_pid), str(self.stub_pid), hook, name]
        check = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 start_new_session=True)
        try:
            if signals:
                started = time.monotonic()
                while not self.stub_pid.exists() and time.monotonic() - started < 20:
                    time.sleep(0.1)
                self.assertTrue(self.stub_pid.exists(), 'the check did not start the server')
                for delay, sent in signals:
                    time.sleep(delay)
                    subprocess.run(prefix + ['kill', '-s', sent.removeprefix('SIG'),
                                             self.apply_pid.read_text().strip()], check=True)
            said = check.communicate(timeout=LIMIT)[0]
        except subprocess.TimeoutExpired:
            check.kill()
            said = check.communicate()[0]
            self.fail(f'apply still runs {LIMIT} s on: {said}')
        if hook != 'none':
            self.assertIn('hook ' + hook, said, 'the check did not reach the phase')
        self.assertTrue(self.stub_pid.exists(), 'the check did not start the server')
        self.assertNotEqual(check.returncode, 0, said)
        self.assertNotIn('installed', said)
        time.sleep(1)
        server = self.stub_pid.read_text().strip()
        self.assertFalse(Path('/proc', server).exists(), f'the server {server} still runs: {said}')
        left = subprocess.run(['pgrep', '-a', '-f', str(self.work)], capture_output=True, text=True).stdout
        self.assertEqual(left, '', 'still running')
        left = subprocess.run(candidate.AS_ROOT + ['find', str(self.scratch), '-mindepth', '1', '-type', 'f'],
                              capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(Path(path).name for path in left), [], 'keys left')
        self.assertEqual(sorted(path.name for path in self.scratch.iterdir()), [], 'scratch left')
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ['ccd', 'pki', 'server.conf'])
        self.assertEqual((self.base / 'server.conf').read_text(), WORKING)

    def each(self, prefix, cases):
        for kind, args in cases:
            with self.subTest(**kind):
                try:
                    self.stopped(prefix, *args)
                finally:
                    self.sweep()

    def as_root(self):
        return [] if os.geteuid() == 0 else ['sudo', '-n']

    # Edge 1: the candidate goes with the check.
    def signal_during_check(self, prefix):
        self.each(prefix, [({'signal': name}, (self.stub(), 'none', name, [(2, name)]))
                           for name in ('SIGTERM', 'SIGHUP', 'SIGINT')])

    def test_signal_during_check_leaves_no_candidate_as_root(self):
        self.signal_during_check(self.as_root())

    @unittest.skipIf(os.geteuid() == 0, 'runs as root')
    def test_signal_during_check_leaves_no_candidate_through_sudo(self):
        self.signal_during_check([])

    # Edge 2: the server is up, the deadline is not.
    def signal_right_after_start(self, prefix):
        self.each(prefix, [({'signal': name, 'deaf_to_sigterm': deaf}, (self.stub(deaf), 'timer', name))
                           for name, deaf in (('SIGTERM', False), ('SIGHUP', False), ('SIGINT', False),
                                              ('SIGTERM', True))])

    def test_signal_right_after_server_start_leaves_nothing_as_root(self):
        self.signal_right_after_start(self.as_root())

    @unittest.skipIf(os.geteuid() == 0, 'runs as root')
    def test_signal_right_after_server_start_leaves_nothing_through_sudo(self):
        self.signal_right_after_start([])

    # Edge 3 (a): a second signal while a deaf server is being stopped.
    def second_signal_during_cleanup(self, prefix):
        self.each(prefix, [({'signals': f'{first}+{second}'},
                            (self.stub(deaf=True), 'none', first, [(2, first), (1, second)]))
                           for first, second in (('SIGINT', 'SIGINT'), ('SIGTERM', 'SIGINT'),
                                                 ('SIGHUP', 'SIGINT'), ('SIGTERM', 'SIGTERM'))])

    def test_second_signal_during_cleanup_leaves_nothing_as_root(self):
        self.second_signal_during_cleanup(self.as_root())

    @unittest.skipIf(os.geteuid() == 0, 'runs as root')
    def test_second_signal_during_cleanup_leaves_nothing_through_sudo(self):
        self.second_signal_during_cleanup([])

    # Edge 3 (b): the check passed, its keys are being removed.
    def signal_during_scratch_removal(self, prefix):
        self.each(prefix, [({'signal': name}, (self.stub(starts=True), 'rmtree', name))
                           for name in ('SIGTERM', 'SIGHUP', 'SIGINT')])

    def test_signal_during_scratch_removal_leaves_no_key_as_root(self):
        self.signal_during_scratch_removal(self.as_root())

    @unittest.skipIf(os.geteuid() == 0, 'runs as root')
    def test_signal_during_scratch_removal_leaves_no_key_through_sudo(self):
        self.signal_during_scratch_removal([])


# Edge 4: last good was accepted once; a check that cannot start here does not keep it from coming back.
@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnLastGoodWithoutCheckTests(unittest.TestCase):
    setUp = candidate.OpenvpnCandidateTests.setUp
    refusing = env.OpenvpnCheckEnvironmentTests.refusing
    substituted = env.OpenvpnCheckEnvironmentTests.substituted
    environments = env.OpenvpnCheckEnvironmentTests.environments

    def each(self, test):
        for name, cause, swap, euid, runs in self.environments():
            with self.subTest(environment=name):
                if not runs:
                    self.skipTest(candidate.NEEDS_SERVER)
                # Each environment from a last good the earlier one did not leave half restored.
                self.applier.journal('openvpn').unlink(missing_ok=True)
                (self.base / 'server.conf').write_text(KNOWN)
                self.applier.mark('openvpn')
                self.host.calls.clear()
                test(swap, euid, cause)

    def assertRestored(self, failure):
        self.assertEqual(failure.code, 3, failure)
        self.assertIn('previous config restored', str(failure))
        self.assertEqual((self.base / 'server.conf').read_text(), KNOWN)
        journal = self.applier.journal('openvpn')
        self.assertFalse(journal.exists(), journal.exists() and journal.read_text())
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ['ccd', 'pki', 'server.conf'])

    def test_failed_apply_restores_last_good_where_the_check_cannot_start(self):
        def test(swap, euid, _cause):
            # The fake host keeps a unit with BROKEN in its config down after the restart.
            self.new.write_text(WORKING + '# BROKEN\n')
            with self.substituted(swap, euid), self.assertRaises(apply.Failed) as caught:
                self.applier.install('openvpn', self.new)
            self.assertRestored(caught.exception)
        self.each(test)

    def test_rollback_restores_last_good_where_the_check_cannot_start(self):
        def test(swap, euid, _cause):
            (self.base / 'server.conf').write_text(WORKING + '# BROKEN\n')
            with self.substituted(swap, euid), self.assertRaises(apply.Failed) as caught:
                self.applier.rollback('openvpn')
            self.assertRestored(caught.exception)
        self.each(test)

    def test_candidate_with_that_cipher_is_still_rejected(self):
        def test(swap, euid, cause):
            (self.base / 'server.conf').write_text(WORKING)
            self.applier.mark('openvpn')
            self.new.write_text(KNOWN)
            with self.substituted(swap, euid), self.assertRaises(apply.Failed) as caught:
                self.applier.install('openvpn', self.new)
            self.assertEqual(caught.exception.code, 2, caught.exception)
            self.assertIn('проверка не запустилась', str(caught.exception))
            self.assertIn(cause, str(caught.exception))
            self.assertEqual((self.base / 'server.conf').read_text(), WORKING)
            self.assertEqual(self.host.ran('restart'), [])
        self.each(test)


# Edge 5: a host or container without a tun device skips the server tests, they do not fail there.
class ServerConditionTests(unittest.TestCase):
    def test_no_tun_means_no_server(self):
        real = subprocess.run

        def run(argv, *args, **kwargs):
            if 'tuntap' in argv or '/dev/net/tun' in ' '.join(map(str, argv)):
                return subprocess.CompletedProcess(argv, 1, b'', b'open: No such file or directory')
            return real(['true'], *args, **kwargs)
        with mock.patch.object(candidate.subprocess, 'run', run), \
                mock.patch.object(candidate.shutil, 'which', return_value='/usr/bin/unshare'):
            self.assertFalse(candidate.can_be_root())
        self.assertIn('tun', candidate.NEEDS_SERVER)


if __name__ == '__main__':
    unittest.main()
