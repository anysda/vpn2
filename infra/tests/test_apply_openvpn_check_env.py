"""OpenVPN cipher check: decided right without a tun device, its capabilities or rights, and as a user.

Each environment is a real one, made with unshare and setpriv as root (or sudo -n), or the tests'
own user when it is uid 1000; apply's tests run inside it in a process of their own and first
check that it is what it is named. Where it cannot be made here, it is skipped.
No openvpn, or one that hangs, is named in time.
"""
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_apply import ROOT, apply
import test_apply_openvpn_candidate as candidate
from test_apply_openvpn_cipher_tls import NAMES, STARTS, LeavesNothing, cipher_line

OPENVPN = candidate.OPENVPN
WORKING = candidate.WORKING
AS_ROOT = candidate.AS_ROOT
KNOWN = WORKING + 'cipher AES-256-GCM\n'
GOOD = WORKING + 'cipher BF-CBC\n'
# Each openvpn call of apply has 60 s; the margin is for the rest of install.
LIMIT = 60 + 15
UID = 1000
AS_UID = f'setpriv --reuid={UID} --regid={UID} --clear-groups'
# /dev/net of a mount namespace of its own: empty, or with a tun only root opens.
OWN_DEV_NET = [*AS_ROOT, 'unshare', '--mount', '--propagation', 'private', 'sh', '-c']
ENVIRONMENTS = {
    'no tun device': OWN_DEV_NET + ['mount -t tmpfs none /dev/net && exec "$@"', 'sh'],
    'no CAP_SYS_ADMIN, CAP_NET_ADMIN': [*AS_ROOT, 'setpriv', '--inh-caps=-all', '--ambient-caps=-all',
                                        '--bounding-set=-sys_admin,-net_admin'],
    'no rights on /dev/net/tun': OWN_DEV_NET + ['mount -t tmpfs none /dev/net && mknod -m 600 /dev/net/tun c 10 200 '
                                                f'&& exec {AS_UID} "$@"', 'sh'],
    f'uid {UID}': [] if os.geteuid() == UID else [*AS_ROOT, *AS_UID.split()],
}
INSIDE = 'VPN2_CHECK_ENVIRONMENT'
CAP_NET_ADMIN, CAP_SYS_ADMIN = 12, 21


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


def substituted(swap):
    """apply finds these programs in place of the ones it names."""
    real = subprocess.Popen

    def popen(argv, *args, **kwargs):
        return real([swap.get(word, word) for word in argv], *args, **kwargs)
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(apply.subprocess, 'Popen', popen))
    return stack


@unittest.skipUnless(Path(OPENVPN).exists(), 'needs ' + OPENVPN)
class OpenvpnCheckEnvironmentTests(unittest.TestCase):
    def test_cipher_check_and_last_good_in_each_environment(self):
        # A copy any user reads: the tree may sit in a home only its owner enters.
        copy = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, copy)
        copy.chmod(0o755)
        (copy / 'infra/control').mkdir(parents=True)
        shutil.copy(ROOT / 'infra/control/apply.py', copy / 'infra/control')
        shutil.copytree(Path(__file__).parent, copy / 'infra/tests', ignore=shutil.ignore_patterns('__pycache__'))
        for path in (copy, *copy.rglob('*')):
            path.chmod(0o755 if path.is_dir() else 0o644)
        for name, prefix in ENVIRONMENTS.items():
            with self.subTest(environment=name):
                # Without root or sudo -n, unshare and setpriv cannot make it.
                made = subprocess.run(prefix + ['true'], capture_output=True, text=True) if prefix else None
                if made and made.returncode:
                    self.skipTest(f'cannot be made here: {made.stderr.strip()}')
                ran = subprocess.run(prefix + ['env', '-i', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin', f'{INSIDE}={name}',
                                               f'VPN2_TEST_ROOT={copy}', sys.executable, '-B', '-m', 'unittest', '-v',
                                               f'{Path(__file__).stem}.{Inside.__name__}'],
                                     cwd=copy / 'infra/tests', capture_output=True, text=True, timeout=600)
                self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr[-4000:])
                self.assertNotIn('skipped', ran.stderr)
                self.assertRegex(ran.stderr, r'Ran [1-9]\d* tests')


@unittest.skipUnless(os.environ.get(INSIDE) in ENVIRONMENTS, f'runs inside an environment, {INSIDE} names it')
class Inside(LeavesNothing, unittest.TestCase):
    """apply in one of ENVIRONMENTS: the cipher as a TLS server decides it, last good comes back."""
    setUp_tree = candidate.OpenvpnCandidateTests.setUp
    rejected = candidate.OpenvpnCandidateTests.rejected
    applied = candidate.OpenvpnCandidateTests.applied

    def test_the_environment_is_the_one_named(self):
        name = os.environ[INSIDE]
        tun = Path('/dev/net/tun')
        effective = int(next(line for line in Path('/proc/self/status').read_text().splitlines()
                             if line.startswith('CapEff:')).split()[1], 16)
        if name == 'no tun device':
            self.assertFalse(tun.exists())
        elif name == 'no rights on /dev/net/tun':
            self.assertTrue(tun.exists())
            self.assertFalse(os.access(tun, os.R_OK | os.W_OK))
        elif name.startswith('no CAP'):
            self.assertEqual(effective & (1 << CAP_SYS_ADMIN | 1 << CAP_NET_ADMIN), 0)
        if name in ('no tun device', 'no CAP_SYS_ADMIN, CAP_NET_ADMIN'):
            # Root here: the oracle of the cipher tests cannot start its server, and is skipped.
            self.assertEqual(os.geteuid(), 0)
            self.assertFalse(candidate.can_be_root())
        else:
            self.assertEqual(os.geteuid(), UID)
            self.assertEqual(effective, 0)

    def test_cipher_is_decided_as_a_tls_server_starts(self):
        for name in NAMES:
            with self.subTest(cipher=name, server_starts=STARTS[name]):
                body = WORKING + cipher_line(name)
                if STARTS[name]:
                    self.applied(body)
                    self.host.calls.clear()
                    self.applied(WORKING)
                else:
                    self.rejected(body, 'cipher')
                self.host.calls.clear()
                self.assertLeftNothing()

    def restored(self, failure):
        self.assertEqual(failure.code, 3, failure)
        self.assertNamed(failure, 'previous config restored')
        self.assertEqual((self.base / 'server.conf').read_text(), GOOD)
        journal = self.applier.journal('openvpn')
        self.assertFalse(journal.exists(), journal.exists() and journal.read_text())
        self.assertLeftNothing()

    def test_failed_apply_restores_last_good_with_cipher(self):
        (self.base / 'server.conf').write_text(GOOD)
        self.applier.mark('openvpn')
        # The fake host keeps a unit with BROKEN in its config down after the restart.
        self.new.write_text(WORKING + '# BROKEN\n')
        with self.assertRaises(apply.Failed) as caught:
            self.applier.install('openvpn', self.new)
        self.restored(caught.exception)

    def test_rollback_restores_last_good_with_cipher(self):
        (self.base / 'server.conf').write_text(GOOD)
        self.applier.mark('openvpn')
        (self.base / 'server.conf').write_text(WORKING + '# BROKEN\n')
        with self.assertRaises(apply.Failed) as caught:
            self.applier.rollback('openvpn')
        self.restored(caught.exception)


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
