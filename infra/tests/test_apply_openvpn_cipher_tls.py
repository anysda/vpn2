"""OpenVPN candidate check: a cipher is decided as a TLS server start decides it, wherever apply runs."""
import os
from pathlib import Path
import re
import subprocess
import threading
import unittest
from unittest import mock

from test_apply import apply
import test_apply_openvpn_candidate as candidate

OPENVPN = candidate.OPENVPN
WORKING = candidate.WORKING
AS_ROOT = candidate.AS_ROOT
# openvpn 2.6 and 2.7 decide these differently in --test-crypto and in a TLS server
# start: BF-CBC is the fallback for old clients there, case, "?" and option words included.
NAMES = ('BF-CBC', 'bf-cbc', 'NOPE', 'AES256', 'AES-256-GCM', 'AES-128-CBC', '', '?BF-CBC',
         '--', '--daemon', '--writepid')
# What a real TLS server start (the oracle below) does with each, on 2.6.19 as on 2.7.0.
STARTS = {'BF-CBC': True, 'bf-cbc': False, 'NOPE': False, 'AES256': True, 'AES-256-GCM': True,
          'AES-128-CBC': True, '': False, '?BF-CBC': False, '--': False, '--daemon': False, '--writepid': False}
LISTS = (('data-ciphers', 'BF-CBC'), ('data-ciphers', 'AES-256-GCM:BF-CBC'),
         ('data-ciphers-fallback', 'BF-CBC'))
CYRILLIC = '[А-Яа-яЁё]'
can_be_root = candidate.can_be_root


def cipher_line(name):
    return f'cipher {name}\n'.replace(' \n', '\n')


def traces(work, tmp):
    """What a check could leave on the host: interfaces, routes, processes of the test tree, temporary files."""
    def said(argv):
        try:
            return subprocess.run(argv, capture_output=True, text=True).stdout
        except FileNotFoundError:
            return f'no {argv[0]}'
    return {'interfaces': sorted(re.findall(r'(?m)^\d+: ([^:@]+)', said(['ip', '-o', 'link', 'show']))),
            'routes': sorted(said(['ip', 'route', 'show', 'table', 'all']).splitlines()),
            # [/]tmp/... finds the processes under the work directory, not this pgrep.
            'processes': sorted(said(['pgrep', '-a', '-f', f'[/]{str(work)[1:]}']).splitlines()),
            'temporary': sorted(os.listdir(tmp))}


class LeavesNothing:
    """Each test with a temporary directory of its own, as TMPDIR gives it, and the host as it found it."""

    def setUp(self):
        self.setUp_tree()
        self.tmp = self.work / 'tmp'
        self.tmp.mkdir()
        patch = mock.patch.object(apply.tempfile, 'tempdir', str(self.tmp))
        patch.start()
        self.addCleanup(patch.stop)
        self.before = traces(self.work, self.tmp)

    def assertLeftNothing(self):
        self.assertEqual(traces(self.work, self.tmp), self.before)
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ['ccd', 'pki', 'server.conf'])

    def assertNamed(self, failure, cause):
        """A refusal says why in the words apply prints everywhere else, not as an unknown cipher."""
        text = str(failure)
        self.assertNotIn('unknown cipher', text)
        self.assertIn(cause, text)
        self.assertNotRegex(text, CYRILLIC)


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnCipherTlsTests(LeavesNothing, unittest.TestCase):
    # The temporary PKI and helpers of the cipher tests, not their tests.
    setUp_tree = candidate.OpenvpnCandidateTests.setUp
    rejected = candidate.OpenvpnCandidateTests.rejected
    applied = candidate.OpenvpnCandidateTests.applied

    def server_starts(self, body):
        """The oracle: openvpn starts a TLS server from the config, or exits on it."""
        conf = self.work / 'oracle.conf'
        conf.write_text(body)
        # A network namespace of its own and no DCO: its tun goes with the server.
        server = subprocess.Popen(AS_ROOT + ['unshare', '--net', OPENVPN, '--cd', str(self.base),
                                             '--config', str(conf), '--verb', '3', '--disable-dco'],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        timer = threading.Timer(60, server.kill)
        timer.start()
        said = []
        try:
            for line in server.stdout:
                said.append(line.rstrip())
                if 'Initialization Sequence Completed' in line:
                    return True, said[-1]
            return False, said[-1] if said else f'rc={server.wait()}'
        finally:
            timer.cancel()
            if server.poll() is None:
                server.terminate()
            server.wait()
            server.stdout.close()
            conf.unlink()

    def decided(self, body, option, starts):
        with self.subTest(line=body.removeprefix(WORKING), server_starts=starts):
            if starts:
                try:
                    self.applied(body)
                except apply.Failed as error:
                    self.fail(f'the server starts, the check said: {error}')
                self.host.calls.clear()
                self.applied(WORKING)
            else:
                self.rejected(body, option)
            self.assertLeftNothing()
        self.host.calls.clear()

    @candidate.needs_server
    def test_the_oracle_starts_as_the_table_says(self):
        starts, said = self.server_starts(WORKING)
        self.assertTrue(starts, said)
        for name in NAMES:
            starts, said = self.server_starts(WORKING + cipher_line(name))
            self.assertEqual(starts, STARTS[name], f'cipher {name!r}: {said}')

    def test_cipher_is_decided_as_a_tls_server_starts(self):
        # No root, sudo or tun needed: the decision is the oracle's wherever apply runs.
        for name in NAMES:
            self.decided(WORKING + cipher_line(name), 'cipher', STARTS[name])

    def test_rejected_cipher_is_named(self):
        for name in ('bf-cbc', 'NOPE', '?BF-CBC'):
            with self.subTest(cipher=name):
                self.new.write_text(WORKING + cipher_line(name))
                with self.assertRaises(apply.Failed) as caught:
                    self.applier.install('openvpn', self.new)
                self.assertIn(f'cipher: unknown cipher {name}', str(caught.exception))
                self.assertNotRegex(str(caught.exception), CYRILLIC)

    @candidate.needs_server
    def test_cipher_lists_are_decided_as_a_tls_server_starts(self):
        for option, names in LISTS:
            body = WORKING + f'{option} {names}\n'
            starts, said = self.server_starts(body)
            with self.subTest(server_said=said):
                self.decided(body, option, starts)

    def test_last_good_with_cipher_bf_cbc_is_restored(self):
        good = WORKING + 'cipher BF-CBC\n'
        (self.base / 'server.conf').write_text(good)
        self.applier.mark('openvpn')
        # The fake host keeps a unit with BROKEN in its config down after the restart.
        self.new.write_text(WORKING + '# BROKEN\n')
        with self.assertRaises(apply.Failed) as caught:
            self.applier.install('openvpn', self.new)
        self.assertEqual(caught.exception.code, 3, caught.exception)
        self.assertNamed(caught.exception, 'previous config restored')
        self.assertEqual((self.base / 'server.conf').read_text(), good)
        self.assertLeftNothing()


if __name__ == '__main__':
    unittest.main()
