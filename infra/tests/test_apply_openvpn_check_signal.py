"""OpenVPN cipher check: apply stopped by a signal leaves no server and no key, a known cipher that fails is named."""
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from test_apply import apply
import test_apply_openvpn_candidate as candidate
from test_apply_openvpn_check_env import NO_TUN, script

OPENVPN = candidate.OPENVPN
TESTS = Path(__file__).resolve().parent
# openvpn 2.6/2.7 on a cipher it does not have, as a server started by hand prints it.
NO_CIPHER = ("2026-10-31 12:00:00 DEPRECATED OPTION: --cipher set to '$c' but missing in --data-ciphers (DEFAULT). "
             'OpenVPN ignores --cipher for cipher negotiations.\n'
             '2026-10-31 12:00:00 Cipher $c not supported\n'
             '2026-10-31 12:00:00 Exiting due to fatal error')
STAMP = r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d'
# apply in a process of its own, installing a candidate whose cipher check starts the stub.
RUNNER = '''
import os, subprocess, sys, tempfile
from pathlib import Path
tests, root, state, new, scratch, stub, pid = sys.argv[1:]
sys.path.insert(0, tests)
from test_apply import Host, apply
tempfile.tempdir = scratch
real = subprocess.Popen
def popen(argv, *args, **kwargs):
    if '/usr/bin/unshare' in argv:
        argv = [stub if word == '/usr/sbin/openvpn' else word for word in argv]
    return real(argv, *args, **kwargs)
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
# How the check gets stopped: the signal apply gets, and from whom.
STOPS = (('SIGTERM', 'kill'), ('SIGHUP', 'kill'), ('SIGTERM', 'timeout'), ('SIGINT', 'kill'))
# The check kills a server deaf to SIGTERM 10 s on.
LIMIT = 10 + 20


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
@candidate.needs_server
class OpenvpnCheckSignalTests(unittest.TestCase):
    setUp_tree = candidate.OpenvpnCandidateTests.setUp

    def setUp(self):
        self.setUp_tree()
        self.addCleanup(self.sweep)
        self.scratch = self.work / 'scratch'
        self.scratch.mkdir()
        self.apply_pid, self.stub_pid = self.work / 'apply.pid', self.work / 'stub.pid'
        self.new.write_text(candidate.WORKING + 'cipher BF-CBC\n')
        # Root's leftovers must not keep the tree from going.
        self.addCleanup(subprocess.run, ['sudo', '-n', 'chmod', '-R', 'a+rwX', str(self.work)], capture_output=True)

    def sweep(self):
        """Whatever a check under test left: processes of this tree and root's scratch."""
        # [/]tmp/... finds the processes under the work directory, not this pkill.
        subprocess.run(candidate.AS_ROOT + ['pkill', '-KILL', '-f', f'[/]{str(self.work)[1:]}'], capture_output=True)
        if self.stub_pid.exists():
            subprocess.run(candidate.AS_ROOT + ['kill', '-KILL', self.stub_pid.read_text().strip()],
                           capture_output=True)
        subprocess.run(candidate.AS_ROOT + ['find', str(self.scratch), '-mindepth', '1', '-delete'],
                       capture_output=True)
        for path in (self.apply_pid, self.stub_pid):
            path.unlink(missing_ok=True)

    def stub(self, deaf):
        """A server that never says it started; deaf, it does not exit on SIGTERM either."""
        trap = "trap '' TERM\n" if deaf else ''
        return script(self.work / 'openvpn', f'echo $$ > {self.stub_pid}\n{trap}exec sleep 1000')

    def stopped(self, prefix, name, sender, stub):
        runner = self.work / 'runner.py'
        runner.write_text(RUNNER)
        argv = prefix + [sys.executable, '-B', '-I', str(runner), str(TESTS), str(self.root), str(self.state),
                         str(self.new), str(self.scratch), stub, str(self.apply_pid)]
        if sender == 'timeout':
            # An outer timeout, as a dropped SSH session or a job limit stops 29-control.sh.
            argv = ['timeout', '-s', name.removeprefix('SIG'), '8'] + argv
        check = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 start_new_session=True)
        try:
            if sender == 'kill':
                started = time.monotonic()
                while not self.stub_pid.exists() and time.monotonic() - started < 20:
                    time.sleep(0.1)
                self.assertTrue(self.stub_pid.exists(), 'the check did not start the server')
                time.sleep(2)
                subprocess.run(prefix + ['kill', '-s', name.removeprefix('SIG'),
                                         self.apply_pid.read_text().strip()], check=True)
            said = check.communicate(timeout=LIMIT)[0]
        except subprocess.TimeoutExpired:
            check.kill()
            said = check.communicate()[0]
            self.fail(f'apply still runs {LIMIT} s after {name}: {said}')
        self.assertTrue(self.stub_pid.exists(), f'the check did not start the server before {name}')
        self.assertNotEqual(check.returncode, 0, said)
        self.assertNotIn('installed', said)
        time.sleep(1)
        server = self.stub_pid.read_text().strip()
        self.assertFalse(Path('/proc', server).exists(), f'the server {server} still runs after {name}')
        left = subprocess.run(['pgrep', '-a', '-f', stub], capture_output=True, text=True).stdout
        self.assertEqual(left, '', f'still running after {name}')
        self.assertEqual(sorted(path.name for path in self.scratch.iterdir()), [], f'left after {name}')

    def stops_clean(self, prefix):
        for deaf in (False, True):
            for name, sender in STOPS:
                with self.subTest(signal=name, sender=sender, deaf_to_sigterm=deaf):
                    try:
                        self.stopped(prefix, name, sender, self.stub(deaf))
                    finally:
                        self.sweep()

    def test_apply_stopped_as_root_leaves_no_server_and_no_key(self):
        self.stops_clean([] if os.geteuid() == 0 else ['sudo', '-n'])

    @unittest.skipIf(os.geteuid() == 0, 'runs as root')
    def test_apply_stopped_through_sudo_leaves_no_server_and_no_key(self):
        self.stops_clean([])


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
        self.assertEqual((self.base / 'server.conf').read_text(), candidate.WORKING)

    def test_known_cipher_that_does_not_start_is_named_by_what_openvpn_said(self):
        # unshare and the server in one stub, so the check runs it as root would without being root.
        cipher = 'c=\nwhile [ $# -gt 0 ]; do [ "$1" = --cipher ] && c=$2; shift; done\n'
        for name, said, cause in (('no cipher', NO_CIPHER, 'Cipher AES-256-GCM not supported'),
                                  ('no tun', NO_TUN, 'Cannot open TUN/TAP dev /dev/net/tun')):
            with self.subTest(openvpn=name):
                lines = ''.join(f'echo "{line}"\n' for line in said.split('\n'))
                stub = script(self.work / 'unshare', cipher + lines + 'exit 1')
                real = subprocess.Popen

                def popen(argv, *args, **kwargs):
                    return real([stub if word == '/usr/bin/unshare' else word for word in argv], *args, **kwargs)
                self.new.write_text(candidate.WORKING + 'cipher BF-CBC\n')
                with mock.patch.object(apply.subprocess, 'Popen', popen), \
                        mock.patch.object(apply.os, 'geteuid', return_value=0), \
                        self.assertRaises(apply.Failed) as caught:
                    self.applier.install('openvpn', self.new)
                self.assertCauseNamed(caught.exception, cause)

    @candidate.needs_server
    def test_known_cipher_the_real_openvpn_lacks_is_named(self):
        self.new.write_text(candidate.WORKING + 'cipher NOPE\n')
        with mock.patch.object(apply, 'KNOWN_CIPHER', 'NOPE2'), self.assertRaises(apply.Failed) as caught:
            self.applier.install('openvpn', self.new)
        self.assertCauseNamed(caught.exception, 'Cipher NOPE2 not supported')


if __name__ == '__main__':
    unittest.main()
