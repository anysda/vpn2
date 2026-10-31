"""OpenVPN cipher check: decided right without tun, sudo or unshare; no openvpn or one that hangs is named in time."""
import contextlib
from pathlib import Path
import subprocess
import time
import unittest
from unittest import mock

from test_apply import apply
import test_apply_openvpn_candidate as candidate
from test_apply_openvpn_cipher_tls import NAMES, STARTS, LeavesNothing, cipher_line

OPENVPN = candidate.OPENVPN
WORKING = candidate.WORKING
KNOWN = WORKING + 'cipher AES-256-GCM\n'
GOOD = WORKING + 'cipher BF-CBC\n'
# What each refusal of the environment prints, as unshare and sudo say it.
NO_UNSHARE = 'unshare: unshare failed: Operation not permitted'
NO_SUDO = 'sudo: a password is required'
# Each openvpn call of apply has 60 s; the margin is for the rest of install.
LIMIT = 60 + 15


def script(path, body):
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o755)
    return str(path)


def hanging(work, deaf=False):
    """openvpn that says nothing and does not exit once started as a server; anything else is the real one.

    Its pid goes to work/server.pid. Deaf, it does not exit on SIGTERM either.
    """
    trap = "trap '' TERM\n" if deaf else ''
    return script(work / 'openvpn', f'for word; do [ "$word" = --server ] && break; done\n'
                                    f'[ "$word" = --server ] || exec {OPENVPN} "$@"\n'
                                    f'echo $$ > {work / "server.pid"}\n{trap}exec sleep 1000')


def substituted(swap, euid=None):
    """apply finds these programs in place of the ones it names, as on a host that has them so."""
    real = subprocess.Popen

    def popen(argv, *args, **kwargs):
        return real([swap.get(word, word) for word in argv], *args, **kwargs)
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(apply.subprocess, 'Popen', popen))
    if euid is not None:
        # apply runs as this user: root without privileges, or a user.
        stack.enter_context(mock.patch.object(apply.os, 'geteuid', return_value=euid))
    return stack


class Environments:
    def refusing(self, name, said):
        return script(self.work / name, f"echo '{said}' >&2\nexit 1")

    def environments(self):
        """(name, programs as the host has them, euid apply runs with): no server can start there."""
        return (
            ('root without unshare', {'/usr/bin/unshare': self.refusing('unshare', NO_UNSHARE)}, 0),
            ('user, sudo asks a password', {'/usr/bin/sudo': self.refusing('sudo', NO_SUDO)}, 1000),
            ('user without sudo', {'/usr/bin/sudo': str(self.work / 'no-sudo')}, 1000))


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnCheckEnvironmentTests(Environments, LeavesNothing, unittest.TestCase):
    setUp_tree = candidate.OpenvpnCandidateTests.setUp
    rejected = candidate.OpenvpnCandidateTests.rejected
    applied = candidate.OpenvpnCandidateTests.applied

    def test_cipher_is_decided_as_the_server_where_no_server_can_start(self):
        for environment, swap, euid in self.environments():
            for name in NAMES:
                with self.subTest(environment=environment, cipher=name, server_starts=STARTS[name]):
                    body = WORKING + cipher_line(name)
                    with substituted(swap, euid):
                        if STARTS[name]:
                            self.applied(body)
                            self.host.calls.clear()
                            self.applied(WORKING)
                        else:
                            self.rejected(body, 'cipher')
                    self.host.calls.clear()
                    self.assertLeftNothing()

    def test_last_good_with_cipher_is_restored_where_no_server_can_start(self):
        for environment, swap, euid in self.environments():
            with self.subTest(environment=environment):
                (self.base / 'server.conf').write_text(GOOD)
                self.applier.mark('openvpn')
                # The fake host keeps a unit with BROKEN in its config down after the restart.
                self.new.write_text(WORKING + '# BROKEN\n')
                with substituted(swap, euid), self.assertRaises(apply.Failed) as caught:
                    self.applier.install('openvpn', self.new)
                self.assertEqual(caught.exception.code, 3, caught.exception)
                self.assertNamed(caught.exception, 'previous config restored')
                self.assertEqual((self.base / 'server.conf').read_text(), GOOD)
                self.assertLeftNothing()
                self.host.calls.clear()


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnCheckRefusedTests(LeavesNothing, unittest.TestCase):
    """What the check cannot decide is said in time, and last good still comes back."""
    setUp_tree = candidate.OpenvpnCandidateTests.setUp

    def server_gone(self):
        pid = self.work / 'server.pid'
        if pid.exists():
            self.assertFalse(Path('/proc', pid.read_text().strip()).exists(), 'the server still runs')
            pid.unlink()

    def install(self, swap, body, cause):
        self.new.write_text(body)
        started = time.monotonic()
        with substituted(swap), self.assertRaises(apply.Failed) as caught:
            self.applier.install('openvpn', self.new)
        took = time.monotonic() - started
        self.assertLessEqual(took, LIMIT, caught.exception)
        self.assertEqual(caught.exception.code, 2, caught.exception)
        self.assertIn('candidate rejected, working config untouched', str(caught.exception))
        self.assertNamed(caught.exception, cause)
        self.assertEqual((self.base / 'server.conf').read_text(), WORKING)
        self.assertEqual(self.host.ran('restart'), [])
        self.server_gone()
        self.assertLeftNothing()

    def restore(self, swap, cause):
        (self.base / 'server.conf').write_text(GOOD)
        self.applier.mark('openvpn')
        (self.base / 'server.conf').write_text(WORKING + '# BROKEN\n')
        started = time.monotonic()
        with substituted(swap), self.assertRaises(apply.Failed) as caught:
            self.applier.rollback('openvpn')
        self.assertLessEqual(time.monotonic() - started, LIMIT, caught.exception)
        self.assertNamed(caught.exception, cause)
        self.server_gone()
        self.assertLeftNothing()
        return caught.exception

    def test_no_openvpn_is_named(self):
        missing = {OPENVPN: str(self.work / 'no-openvpn')}
        for body in (WORKING + 'keepalive 10 60\n', GOOD):
            with self.subTest(line=body.removeprefix(WORKING)):
                self.install(missing, body, 'No such file')

    def test_no_openvpn_in_restore_is_named(self):
        failure = self.restore({OPENVPN: str(self.work / 'no-openvpn')}, 'No such file')
        self.assertEqual(failure.code, 4, failure)

    def test_hanging_server_is_named_in_time(self):
        for deaf in (False, True):
            for body in (GOOD, KNOWN):
                with self.subTest(line=body.removeprefix(WORKING), deaf_to_sigterm=deaf):
                    self.install({OPENVPN: hanging(self.work, deaf)}, body, 'openvpn --cipher')

    def test_last_good_comes_back_while_the_server_hangs(self):
        # Last good was taken once; a check that gives no answer does not keep it out.
        failure = self.restore({OPENVPN: hanging(self.work)}, 'previous config restored')
        self.assertEqual(failure.code, 3, failure)
        self.assertEqual((self.base / 'server.conf').read_text(), GOOD)
        self.assertFalse(self.applier.journal('openvpn').exists())

    def test_working_config_without_cipher_starts_no_server(self):
        # The usual path as on dev: no cipher line, no server start, even one that would hang.
        self.new.write_text(WORKING + 'data-ciphers AES-256-GCM:AES-128-GCM\nauth SHA256\n')
        started = time.monotonic()
        with substituted({OPENVPN: hanging(self.work)}):
            self.applier.install('openvpn', self.new)
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse((self.work / 'server.pid').exists(), 'a server was started')
        self.assertLeftNothing()


if __name__ == '__main__':
    unittest.main()
