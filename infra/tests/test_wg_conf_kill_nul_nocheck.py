"""wg0.conf check: a kill mid-check leaves no key behind, NUL as wg sees it, a hanging Endpoint.

Review 11 of the check that asks wg in a temporary netns found:
1 the backup copy killed (SIGTERM, SIGKILL) after `wg setconf` left the netns
  with wg0, the server key and PSK in it, until reboot;
2 28-wireguard read server.priv keeping NUL, while wg-quick
  drops it: `Public\\0Key`, `[Pe\\0er]`, `Private\\0Key` - wg holds what the stage does not see;
3 an Endpoint with a host name has no network in the netns: wg retries resolving and the
  check died of subprocess.TimeoutExpired after 30 s;
The symptom is checked, not the way: after the kill and a margin the next check runs and no
netns, named or held by a process, has an interface holding the key.
Root via `sudo -n` (wg_root), else skipped.
"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from test_wg_conf_module import BASE, CHECK, CHECK_ENV, bundle, conf
from test_wg_conf_wg_oracle import A, S, backup, mk, oracle
import vpn_roles
from wg_root import as_root

WG, WG_QUICK, IP, NSENTER = shutil.which('wg'), shutil.which('wg-quick'), shutil.which('ip'), shutil.which('nsenter')
LIMIT = 30  # wg_conf.py: the bound of one wg call
MARGIN = 15
# 2 NUL in key names, values and section headers: wg-quick drops it before wg.
NUL_PRIVATE = {
    'priv-nul-name': mk().replace(b'PrivateKey = ', b'Private\x00Key = '),
    'iface-nul-header': mk(header=b'[Inter\x00face]'),
}


def stub_wg(directory, mark):
    """A wg that does the real `setconf`, touches `mark` and holds on: the window after setconf."""
    path = Path(directory) / 'wg'
    path.write_text(f'#!/bin/bash\nif [[ $1 == setconf ]]; then\n  {WG} "$@" || exit\n  : > {mark}\n'
                    f'  exec sleep 60\nfi\nexec {WG} "$@"\n')
    path.chmod(0o755)
    return str(path.parent)


def named():
    return set(os.listdir('/run/netns')) if os.path.isdir('/run/netns') else set()


def holding_key():
    """Every netns, named or held by a process, with an interface whose private key is S."""
    found, seen = [], {os.stat('/proc/self/ns/net').st_ino}
    places = [Path('/run/netns') / name for name in named()]
    places += [proc / 'ns/net' for proc in Path('/proc').iterdir() if proc.name.isdigit()]
    for place in places:
        try:
            inode = os.stat(place).st_ino
        except OSError:
            continue
        if inode in seen:
            continue
        seen.add(inode)
        done = subprocess.run([NSENTER, f'--net={place}', WG, 'show', 'all', 'private-key'], capture_output=True,
                              text=True, timeout=30, check=False)
        if S.decode() in done.stdout:
            found.append(str(place))
    return found


def role_key(raw):
    """The wireguard role's wg-identity step on wg0.conf `raw`: the server.priv it writes."""
    with tempfile.TemporaryDirectory() as directory:
        top = Path(directory)
        (top / 'wg0.conf').write_bytes(raw)
        done = vpn_roles.wg_identity(top)
        assert not done.returncode, done.stderr
        return (top / 'server.priv').read_text().strip()


@as_root
@unittest.skipUnless(WG and WG_QUICK and IP and NSENTER, 'needs wg, wg-quick, ip and nsenter')
class WgCheckEdgeTests(unittest.TestCase):
    """1 a kill mid-check, 2 NUL as wg, 3 an Endpoint name, 4 nothing to check with."""

    def setUp(self):
        self.before = named()
        self.groups = []

    def tearDown(self):
        for name in named() - self.before:
            subprocess.run([IP, 'netns', 'del', name], capture_output=True, check=False)
        self.groups = []

    def test_1_kill_mid_check_leaves_no_key(self):
        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory)
            mark = top / 'setconf-done'
            (top / 'stub').mkdir()
            (top / 'bundle').mkdir()
            path = stub_wg(top / 'stub', mark)
            env = os.environ | {'PATH': path + os.pathsep + os.environ['PATH'], **CHECK_ENV,
                                'PYTHONDONTWRITEBYTECODE': '1'}
            root = bundle(top / 'bundle', conf(BASE))
            (root / 'etc-wireguard/wg0.conf').write_bytes(mk())
            callers = {'backup --check-bundle': [*CHECK, str(root)]}
            for who, command in callers.items():
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    with self.subTest('no netns holds the key after the kill, the next check works',
                                      caller=who, signal=sig.name):
                        mark.unlink(missing_ok=True)
                        child = subprocess.Popen(command, env=env, start_new_session=True,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        self.groups.append(child.pid)
                        deadline = time.monotonic() + 20
                        while not mark.exists() and child.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.assertTrue(mark.exists(), 'the check did not reach the window after setconf')
                        os.killpg(child.pid, sig)
                        child.wait(timeout=30)
                        time.sleep(2)
                        try:
                            self.assertEqual(holding_key(), [])
                            self.assertEqual(named() - self.before, set())
                        finally:
                            self.tearDown()

    def test_2_nul_as_wg_quick_and_wg(self):
        for name, raw in NUL_PRIVATE.items():
            error, private, _peers = oracle(raw)
            self.assertEqual((error, private), (None, S.decode()), name)
            with self.subTest('server.priv is the key wg holds', edge=name):
                self.assertEqual(role_key(raw), private)

    def test_3_endpoint_name_is_a_refusal_in_time(self):
        raw = mk(peer=b'Endpoint = vpn.example.test:51820\n')
        start = time.monotonic()
        code, lines = backup(raw, [(A.decode(), S.decode(), '10.66.66.3', False)])
        took = time.monotonic() - start
        with self.subTest('backup: a refusal naming wg0.conf, no traceback', took=round(took)):
            self.assertFalse([line for line in lines if 'Traceback' in line or 'TimeoutExpired' in line], lines)
            self.assertNotEqual(code, 0, lines)
            self.assertTrue([line for line in lines if 'wg0.conf' in line], lines)
            self.assertLess(took, LIMIT + MARGIN)


if __name__ == '__main__':
    unittest.main()
