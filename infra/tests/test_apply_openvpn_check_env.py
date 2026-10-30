"""OpenVPN cipher check: a server that cannot start or does not stop is not taken for an unknown cipher."""
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_apply import ROOT, apply
import test_apply_openvpn_candidate as candidate
from test_apply_openvpn_cipher_tls import can_be_root

OPENVPN = candidate.OPENVPN
WORKING = candidate.WORKING
KNOWN = WORKING + 'cipher AES-256-GCM\n'
# What each refusal of the environment prints, as unshare, sudo and openvpn 2.6/2.7 say it.
NO_UNSHARE = 'unshare: unshare failed: Operation not permitted'
NO_SUDO = 'sudo: a password is required'
NO_TUN = ('2026-10-31 12:00:00 ERROR: Cannot open TUN/TAP dev /dev/net/tun: No such file or directory (errno=2)\n'
          '2026-10-31 12:00:00 Exiting due to fatal error')
# The server check stops a server after 60 s, then kills it 10 s on.
LIMIT = 60 + 10
# Runs the check in a process of its own, as root or as a user with sudo -n.
RUNNER = '''
import importlib.util, subprocess, sys, tempfile
work, apply_py, stub = sys.argv[1:]
spec = importlib.util.spec_from_file_location('apply', apply_py)
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)
tempfile.tempdir = work + '/scratch'
real = subprocess.Popen
def popen(argv, *args, **kwargs):
    if '/usr/bin/unshare' in argv:
        argv = [stub if word == '/usr/sbin/openvpn' else word for word in argv]
    return real(argv, *args, **kwargs)
subprocess.Popen = popen
try:
    apply.Applier().check_openvpn_names([('cipher', ['AES-256-GCM'])])
except Exception as error:
    print('raised:', error, flush=True)
else:
    print('returned', flush=True)
'''


def script(path, body):
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o755)
    return str(path)


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnCheckEnvironmentTests(unittest.TestCase):
    setUp = candidate.OpenvpnCandidateTests.setUp

    def refusing(self, name, said):
        lines = ''.join(f"echo '{line}'\n" for line in said.split('\n'))
        return script(self.work / name, lines + 'exit 1')

    def substituted(self, swap, euid=None):
        """The server check starts these stubs in place of the programs it names."""
        real = subprocess.Popen

        def popen(argv, *args, **kwargs):
            if '/usr/bin/unshare' in argv:
                argv = [swap.get(word, word) for word in argv]
            return real(argv, *args, **kwargs)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(apply.subprocess, 'Popen', popen))
        if euid is not None:
            stack.enter_context(mock.patch.object(apply.os, 'geteuid', return_value=euid))
        return stack

    def environments(self):
        """(name, cause, programs swapped for stubs, euid apply sees, can run here)."""
        return (
            ('no unshare', 'Operation not permitted',
             {'/usr/bin/unshare': self.refusing('unshare', NO_UNSHARE)}, 0, True),
            ('sudo asks a password', 'a password is required',
             {'/usr/bin/sudo': self.refusing('sudo', NO_SUDO)}, 1000, True),
            ('no tun', 'Cannot open TUN/TAP dev /dev/net/tun',
             {'/usr/sbin/openvpn': self.refusing('openvpn', NO_TUN)}, None, can_be_root()))

    def assertCauseNamed(self, failure, cause):
        self.assertNotIn('unknown cipher', str(failure))
        self.assertIn(cause, str(failure))

    def test_candidate_check_that_cannot_start_names_the_cause(self):
        for name, cause, swap, euid, runs in self.environments():
            with self.subTest(environment=name):
                if not runs:
                    self.skipTest('needs root or sudo -n and unshare --net')
                self.new.write_text(KNOWN)
                with self.substituted(swap, euid), self.assertRaises(apply.Failed) as caught:
                    self.applier.install('openvpn', self.new)
                self.assertEqual(caught.exception.code, 2, caught.exception)
                self.assertIn('candidate rejected, working config untouched', str(caught.exception))
                self.assertCauseNamed(caught.exception, cause)
                self.assertEqual((self.base / 'server.conf').read_text(), WORKING)
                self.assertEqual(self.host.ran('restart'), [])

    def test_last_good_check_that_cannot_start_names_the_cause(self):
        for name, cause, swap, euid, runs in self.environments():
            with self.subTest(environment=name):
                if not runs:
                    self.skipTest('needs root or sudo -n and unshare --net')
                # Each environment from a last good the earlier one did not leave half restored.
                self.applier.journal('openvpn').unlink(missing_ok=True)
                (self.base / 'server.conf').write_text(KNOWN)
                self.applier.mark('openvpn')
                # The fake host keeps a unit with BROKEN in its config down after the restart.
                self.new.write_text(WORKING + '# BROKEN\n')
                with self.substituted(swap, euid), self.assertRaises(apply.Failed) as caught:
                    self.applier.install('openvpn', self.new)
                self.assertIn(caught.exception.code, (3, 4), caught.exception)
                self.assertCauseNamed(caught.exception, cause)
                self.host.calls.clear()


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
@unittest.skipUnless(can_be_root(), 'needs root or sudo -n and unshare --net')
class OpenvpnCheckStopsTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.work = Path(work.name)
        (self.work / 'scratch').mkdir()
        self.addCleanup(work.cleanup)
        # Root's leftovers first: the scratch the check made and anything still running.
        self.addCleanup(subprocess.run, ['sudo', '-n', 'rm', '-rf', str(self.work / 'scratch')])
        self.pid = self.work / 'stub.pid'
        # [/]tmp/... finds the processes under the work directory, not this pkill; the stub is a sleep.
        self.addCleanup(subprocess.run, ['sudo', '-n', 'pkill', '-KILL', '-f', f'[/]{str(self.work)[1:]}'])
        self.addCleanup(lambda: self.pid.exists() and subprocess.run(
            ['sudo', '-n', 'kill', '-KILL', self.pid.read_text().strip()], capture_output=True))
        # A server that says nothing and does not exit on SIGTERM.
        self.stub = script(self.work / 'openvpn', f"echo $$ > {self.pid}\ntrap '' TERM\nexec sleep 1000")
        runner = self.work / 'runner.py'
        runner.write_text(RUNNER)
        self.runner = [sys.executable, '-B', '-I', str(runner), str(self.work),
                       str(ROOT / 'infra/control/apply.py'), self.stub]

    def stopped_in_time(self, prefix):
        started = time.monotonic()
        check = subprocess.Popen(prefix + self.runner, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, start_new_session=True)
        try:
            said = check.communicate(timeout=LIMIT + 15)[0]
        except subprocess.TimeoutExpired:
            said = None
            check.stdout.close()
        took = time.monotonic() - started
        self.assertIsNotNone(said, f'the check still runs after {took:.0f} s')
        self.assertIn('raised:', said)
        self.assertLessEqual(took, LIMIT + 15)
        time.sleep(1)
        pid = self.pid.read_text().strip()
        self.assertFalse(Path('/proc', pid).exists(), f'the server {pid} still runs')
        self.assertEqual(sorted(path.name for path in (self.work / 'scratch').iterdir()), [])

    def test_server_ignoring_sigterm_is_killed_as_root(self):
        self.stopped_in_time([] if os.geteuid() == 0 else ['sudo', '-n'])

    @unittest.skipIf(os.geteuid() == 0, 'runs as root')
    def test_server_ignoring_sigterm_is_killed_through_sudo(self):
        self.stopped_in_time([])


if __name__ == '__main__':
    unittest.main()
