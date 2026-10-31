"""OpenVPN cipher check: apply stopped while it decides a cipher exits 128+sig and leaves nothing."""
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from test_apply import apply
import test_apply_openvpn_candidate as candidate
from test_apply_openvpn_check_env import hanging, script

OPENVPN = candidate.OPENVPN
TESTS = Path(__file__).resolve().parent
# openvpn 2.6/2.7 on a cipher it does not have, as a server started by hand prints it.
NO_CIPHER = ("2026-10-31 12:00:00 DEPRECATED OPTION: --cipher set to '$c' but missing in --data-ciphers (DEFAULT). "
             'OpenVPN ignores --cipher for cipher negotiations.\n'
             '2026-10-31 12:00:00 Cipher $c not supported\n'
             '2026-10-31 12:00:00 Exiting due to fatal error')
STAMP = r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d'
# apply in a process of its own, installing a candidate whose cipher check meets a server that hangs.
# The shell around it prints the exit code as a caller of apply (29-control.sh) sees it.
RUNNER = '''
import os, subprocess, sys, tempfile
from pathlib import Path
tests, root, state, new, scratch, stub, pid = sys.argv[1:]
sys.path.insert(0, tests)
from test_apply import Host, apply
tempfile.tempdir = scratch
real = subprocess.Popen
def popen(argv, *args, **kwargs):
    return real([stub if word == '/usr/sbin/openvpn' else word for word in argv], *args, **kwargs)
subprocess.Popen = popen
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
SHELL = ['/bin/sh', '-c', '"$@"; echo "rc=$?"', 'sh']
# How apply gets stopped: the signal, and from whom.
STOPS = (('SIGTERM', 'kill'), ('SIGHUP', 'kill'), ('SIGINT', 'kill'), ('SIGTERM', 'timeout'))
LIMIT = 30


class Stopped:
    """apply in a process of its own, stopped by signals while the server it checks with hangs."""

    def setUp(self):
        self.setUp_tree()
        self.addCleanup(self.sweep)
        self.scratch = self.work / 'scratch'
        self.scratch.mkdir()
        self.apply_pid, self.server_pid = self.work / 'apply.pid', self.work / 'server.pid'
        self.new.write_text(candidate.WORKING + 'cipher BF-CBC\n')
        # Root's leftovers must not keep the tree from going.
        self.addCleanup(subprocess.run, candidate.AS_ROOT + ['chmod', '-R', 'a+rwX', str(self.work)],
                        capture_output=True)

    def sweep(self):
        """Whatever a check under test left: processes of this tree, the scratch and the candidate."""
        # [/]tmp/... finds the processes under the work directory, not this pkill.
        for prefix in ([], candidate.AS_ROOT):
            subprocess.run(prefix + ['pkill', '-KILL', '-f', f'[/]{str(self.work)[1:]}'], capture_output=True)
            subprocess.run(prefix + ['find', str(self.scratch), '-mindepth', '1', '-delete'], capture_output=True)
            subprocess.run(prefix + ['rm', '-f', str(self.applier.candidate('openvpn'))], capture_output=True)
        for path in (self.apply_pid, self.server_pid):
            path.unlink(missing_ok=True)

    def stopped(self, prefix, signals, sender='kill', deaf=False):
        """apply stopped by SIGNALS, (delay, name) each, once the server is up; the code of the first."""
        runner = self.work / 'runner.py'
        runner.write_text(RUNNER)
        argv = [sys.executable, '-B', '-I', str(runner), str(TESTS), str(self.root), str(self.state),
                str(self.new), str(self.scratch), hanging(self.work, deaf), str(self.apply_pid)]
        name = signals[0][1]
        if sender == 'timeout':
            # An outer timeout, as a dropped SSH session or a job limit stops 29-control.sh.
            argv = ['timeout', '--preserve-status', '-s', name.removeprefix('SIG'), '4'] + argv
        check = subprocess.Popen(prefix + SHELL + argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, start_new_session=True)
        try:
            started = time.monotonic()
            while not self.server_pid.exists() and time.monotonic() - started < 20:
                time.sleep(0.1)
            self.assertTrue(self.server_pid.exists(), 'the check did not start the server')
            for number, (delay, sent) in enumerate(signals if sender == 'kill' else ()):
                time.sleep(delay)
                # A later signal may find apply already gone.
                subprocess.run(prefix + ['kill', '-s', sent.removeprefix('SIG'), self.apply_pid.read_text().strip()],
                               check=not number, capture_output=bool(number))
            said = check.communicate(timeout=LIMIT)[0]
        except subprocess.TimeoutExpired:
            check.kill()
            said = check.communicate()[0]
            self.fail(f'apply still runs {LIMIT} s on: {said}')
        self.assertIn(f'rc={128 + getattr(apply.signal, name)}', said)
        self.assertNotIn('installed', said)
        time.sleep(1)
        server = self.server_pid.read_text().strip()
        self.assertFalse(Path('/proc', server).exists(), f'the server {server} still runs: {said}')
        left = subprocess.run(['pgrep', '-a', '-f', f'[/]{str(self.work)[1:]}'], capture_output=True, text=True)
        self.assertEqual(left.stdout, '', 'still running')
        left = subprocess.run(candidate.AS_ROOT + ['find', str(self.scratch), '-mindepth', '1'],
                              capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(Path(path).name for path in left), [], 'temporary files left')
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ['ccd', 'pki', 'server.conf'])
        self.assertEqual((self.base / 'server.conf').read_text(), candidate.WORKING)

    def each(self, prefix, cases):
        for kind, args in cases:
            with self.subTest(**kind):
                try:
                    self.stopped(prefix, *args)
                finally:
                    self.sweep()


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnCheckSignalTests(Stopped, unittest.TestCase):
    setUp_tree = candidate.OpenvpnCandidateTests.setUp

    def stops_clean(self, prefix):
        self.each(prefix, [({'signal': name, 'sender': sender, 'deaf_to_sigterm': deaf},
                            ([(2, name)], sender, deaf))
                           for deaf in (False, True) for name, sender in STOPS])

    def test_apply_stopped_leaves_no_server_and_no_temporary_file(self):
        self.stops_clean([])

    @unittest.skipIf(os.geteuid() == 0, 'runs as root')
    @unittest.skipUnless(candidate.AS_ROOT and subprocess.run(candidate.AS_ROOT + ['true']).returncode == 0,
                         'needs sudo -n')
    def test_apply_stopped_as_root_leaves_no_server_and_no_temporary_file(self):
        self.stops_clean(candidate.AS_ROOT)


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnKnownCipherFailsTests(unittest.TestCase):
    setUp = candidate.OpenvpnCandidateTests.setUp

    def assertCauseNamed(self, failure, cause):
        text = str(failure)
        self.assertEqual(failure.code, 2, text)
        self.assertIn('candidate rejected, working config untouched', text)
        self.assertNotIn('unknown cipher', text)
        self.assertIn(cause, text)
        self.assertNotIn('Exiting due to fatal error', text)
        self.assertNotRegex(text, STAMP)
        self.assertNotRegex(text, '[А-Яа-яЁё]')
        self.assertEqual((self.base / 'server.conf').read_text(), candidate.WORKING)

    def test_known_cipher_openvpn_refuses_is_named_by_what_openvpn_said(self):
        # A server start of openvpn that has no cipher at all; the rest is the real openvpn.
        lines = ''.join(f'echo "{line}"\n' for line in NO_CIPHER.split('\n'))
        stub = script(self.work / 'openvpn', 'c=\nfor word; do [ "$prev" = --cipher ] && c=$word; prev=$word; done\n'
                      f'[ -n "$c" ] || exec {OPENVPN} "$@"\n{lines}exit 1')
        real = subprocess.Popen

        def popen(argv, *args, **kwargs):
            return real([stub if word == OPENVPN else word for word in argv], *args, **kwargs)
        self.new.write_text(candidate.WORKING + 'cipher BF-CBC\n')
        with mock.patch.object(apply.subprocess, 'Popen', popen), self.assertRaises(apply.Failed) as caught:
            self.applier.install('openvpn', self.new)
        self.assertCauseNamed(caught.exception, 'Cipher AES-256-GCM not supported')

    def test_known_cipher_the_real_openvpn_lacks_is_named(self):
        self.new.write_text(candidate.WORKING + 'cipher NOPE\n')
        with mock.patch.object(apply, 'KNOWN_CIPHER', 'NOPE2'), self.assertRaises(apply.Failed) as caught:
            self.applier.install('openvpn', self.new)
        self.assertCauseNamed(caught.exception, 'Cipher NOPE2 not supported')


if __name__ == '__main__':
    unittest.main()
