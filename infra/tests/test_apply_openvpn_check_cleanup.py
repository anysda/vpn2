"""OpenVPN apply: a signal the caller ignores stays ignored, one in a cleanup leaves no keys and no candidate."""
import errno
from pathlib import Path
import signal
import subprocess
import sys
import unittest

import test_apply_openvpn_candidate as candidate
from test_apply_openvpn_check_signal import TESTS

OPENVPN = candidate.OPENVPN
WORKING = candidate.WORKING
# apply install in a process of its own that sends itself a signal at the named points, each once:
# server - an openvpn server start of the cipher check, scratch - the first file the check's
# temporary directory loses, candidate - the candidate about to go, replacing - the journal just
# written. no-space - the journal cannot be written.
RUNNER = '''
import errno, os, signal, sys, tempfile
from pathlib import Path
tests, root, state, new, scratch, where, name = sys.argv[1:]
sys.path.insert(0, tests)
from test_apply import Host, apply
tempfile.tempdir = scratch
number, where, fired = getattr(signal, name), where.split(','), []
print('ignored:', [each.name for each in apply.SIGNALS if signal.getsignal(each) is signal.SIG_IGN], flush=True)
def fire(point):
    if point in where and point not in fired:
        fired.append(point)
        os.kill(os.getpid(), number)
unlink, path_unlink, stage, server_end = os.unlink, Path.unlink, apply.Applier.stage, apply.tls_server_end
def os_unlink(path, *args, **kwargs):
    # A temporary directory goes by names relative to its descriptor.
    folder = os.readlink(f'/proc/self/fd/{kwargs["dir_fd"]}') if kwargs.get('dir_fd') is not None \\
        else os.path.dirname(os.path.abspath(path))
    if folder.startswith(scratch + os.sep):
        fire('scratch')
    return unlink(path, *args, **kwargs)
def candidate_unlink(self, *args, **kwargs):
    if self.name == '.vpn2-apply-server.conf.new':
        fire('candidate')
    return path_unlink(self, *args, **kwargs)
def journal_stage(self, resource, value):
    if value == 'replacing' and 'no-space' in where:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    stage(self, resource, value)
    if value == 'replacing':
        fire('replacing')
def server(*args):
    fire('server')
    return server_end(*args)
os.unlink, Path.unlink, apply.Applier.stage, apply.tls_server_end = os_unlink, candidate_unlink, journal_stage, server
applier = apply.Applier(Path(root), Path(state), Host(Path(root)))
applier.settle = 0
try:
    try:
        applier.install('openvpn', new)
    finally:
        print('fired:', fired, flush=True)
except apply.Failed as error:
    print('failed:', error, flush=True)
    sys.exit(error.code)
print('done, still ignored:', signal.getsignal(number) is signal.SIG_IGN, flush=True)
'''
SIGNALS = ('SIGTERM', 'SIGHUP', 'SIGINT')
LIMIT = 60


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnStopInCleanupTests(unittest.TestCase):
    setUp_tree = candidate.OpenvpnCandidateTests.setUp
    maxDiff = None

    def setUp(self):
        self.setUp_tree()
        self.scratch = self.work / 'scratch'
        self.scratch.mkdir()
        self.runner = self.work / 'runner.py'
        self.runner.write_text(RUNNER)
        (self.base / 'server.conf').write_text(WORKING)
        self.applier.mark('openvpn')

    def install(self, cipher, points, name, launch=()):
        """Install a config with CIPHER, signal NAME sent at POINTS; the code, what apply said and its traces."""
        self.new.write_text(WORKING + f'cipher {cipher}\n')
        done = subprocess.run([*launch, sys.executable, '-B', '-I', str(self.runner), str(TESTS), str(self.root),
                               str(self.state), str(self.new), str(self.scratch), ','.join(points), name],
                              capture_output=True, text=True, timeout=LIMIT, cwd=str(self.work))
        said = done.stdout + done.stderr
        lines = dict(line.split(': ', 1) for line in done.stdout.splitlines() if ': ' in line)
        traces = {'fired': lines.get('fired'),
                  'temporary directory': sorted(str(path.relative_to(self.scratch)) for path in self.scratch.rglob('*')),
                  'candidate': sorted(path.name for path in self.base.glob('.vpn2-apply-*')),
                  'state': sorted(path.name for path in self.state.iterdir()),
                  'done': 'done, still ignored' in lines}
        return done.returncode, said, lines, traces

    def sweep(self):
        for path in [*self.scratch.rglob('*')][::-1]:
            path.rmdir() if path.is_dir() else path.unlink()
        for path in self.base.glob('.vpn2-apply-*'):
            path.unlink()
        self.applier.journal('openvpn').unlink(missing_ok=True)
        (self.base / 'server.conf').write_text(WORKING)
        self.applier.mark('openvpn')

    def test_signal_the_caller_ignores_stays_ignored(self):
        # nohup ignores HUP, a shell without job control INT for a command run with "&".
        for launch, name in ((['nohup'], 'SIGHUP'), (['/bin/sh', '-c', '"$@" & wait $!', 'sh'], 'SIGINT')):
            with self.subTest(launch=launch[0], signal=name):
                try:
                    code, said, lines, traces = self.install('BF-CBC', ('server', 'scratch', 'replacing'), name, launch)
                    self.assertIn(name, lines.get('ignored', ''), said)
                    self.assertEqual({'code': code, 'still ignored': lines.get('done, still ignored'), **traces,
                                      'working config': (self.base / 'server.conf').read_text()},
                                     {'code': 0, 'still ignored': 'True', 'fired': "['server', 'scratch', 'replacing']",
                                      'temporary directory': [], 'candidate': [], 'state': ['openvpn.good'],
                                      'done': True, 'working config': self.new.read_text()}, said)
                finally:
                    self.sweep()

    def stopped_clean(self, cipher, points, outcome, name):
        """apply with CIPHER stopped by NAME at POINTS: 128+sig or OUTCOME, and no trace either way."""
        number = getattr(signal, name)
        code, said, _, traces = self.install(cipher, points, name)
        self.assertIn(code, (128 + number, outcome), said)
        applied = code == 0
        self.assertEqual({**traces, 'working config': (self.base / 'server.conf').read_text()},
                         {'fired': str([point for point in points if point != 'no-space']),
                          'temporary directory': [], 'candidate': [], 'state': ['openvpn.good'], 'done': applied,
                          'working config': self.new.read_text() if applied else WORKING}, said)

    def test_signal_while_the_check_clears_its_keys_leaves_no_directory(self):
        for cipher in ('BF-CBC', 'NO-SUCH-CIPHER'):
            outcome, said, _, traces = self.install(cipher, (), 'SIGTERM')
            self.sweep()
            self.assertEqual((outcome, traces['temporary directory']), ({'BF-CBC': 0}.get(cipher, 2), []), said)
            for name in SIGNALS:
                with self.subTest(cipher=cipher, signal=name):
                    try:
                        self.stopped_clean(cipher, ('scratch',), outcome, name)
                    finally:
                        self.sweep()

    def test_signal_before_the_candidate_goes_on_an_error_leaves_no_candidate(self):
        # The candidate goes in the except clause of a rejected check and of a journal not written.
        disk_full = 1  # OSError out of apply: a traceback, code 1
        for case, cipher, points, outcome in (('rejected cipher', 'NO-SUCH-CIPHER', ('candidate',), 2),
                                              (errno.errorcode[errno.ENOSPC], 'BF-CBC', ('no-space', 'candidate'),
                                               disk_full)):
            for name in SIGNALS:
                with self.subTest(case=case, signal=name):
                    try:
                        self.stopped_clean(cipher, points, outcome, name)
                    finally:
                        self.sweep()


if __name__ == '__main__':
    unittest.main()
