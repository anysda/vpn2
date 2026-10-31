"""wg0.conf check: a kill mid-check leaves no key behind, NUL as wg sees it, a hanging Endpoint, nothing to check with.

Review 11 of the check that asks wg in a temporary netns found:
1 the backup copy or verify killed (SIGTERM, SIGKILL) after `wg setconf` left the netns
  with wg0, the server key and PSK in it, until reboot;
2 restore cut peers and read server.priv (restore, 28-wireguard) keeping NUL, while wg-quick
  drops it: `Public\\0Key`, `[Pe\\0er]`, `Private\\0Key` - wg holds what restore does not see;
3 an Endpoint with a host name has no network in the netns: wg retries resolving and the
  check died of subprocess.TimeoutExpired after 30 s;
4 nothing to check with (no rights on a netns, no wg) went out as "БД и конфиги VPN
  расходятся — перевыпусти", in the backup event and in restore.
The symptom is checked, not the way: after the kill and a margin the next check runs and no
netns, named or held by a process, has an interface holding the key.
Root via `sudo -n` (wg_root), else skipped.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from test_wg_conf_allowedips_prefix import VT, cut, source
from test_wg_conf_module import BASE, LIB, bundle, conf, restore_check
from test_wg_conf_wg_oracle import A, B, C, S, backup, mk, oracle, restore_cut, without_wg
from wg_root import as_root

WG, WG_QUICK, IP, NSENTER = shutil.which('wg'), shutil.which('wg-quick'), shutil.which('ip'), shutil.which('nsenter')
TESTS = Path(__file__).resolve().parent
LIMIT = 30  # wg_conf.py: the bound of one wg call
MARGIN = 15
PANEL = 'http://127.0.0.1:51821/api/bot/events'
# 2 NUL in key names, values and section headers: wg-quick drops it before wg.
NUL_PEERS = {
    'pub-nul-name': mk().replace(b'PublicKey = ' + A, b'Public\x00Key = ' + A),
    'pub-nul-value': mk().replace(b'PublicKey = ' + B, b'PublicKey = ' + B + b'\x00'),
    'peer-nul-header': mk(tail=b'[Pe\x00er]\nPublicKey = ' + C + b'\nAllowedIPs = 10.66.66.9/32\n'),
}
NUL_PRIVATE = {
    'priv-nul-name': mk().replace(b'PrivateKey = ', b'Private\x00Key = '),
    'iface-nul-header': mk(header=b'[Inter\x00face]'),
}
# Verify's path to the check, in a process of its own.
VERIFY = ('import sys; sys.path.insert(0, sys.argv[1]); from test_wg_conf_allowedips_prefix import VT; '
          'VT.apply_peers(open(sys.argv[2], "rb").read())')


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


def backup_run(text, path):
    """anysda-backup.sh: its snapshot loop and end on a bundle of `text`, the panel event caught.

    The script's own blocks: the head with report_failure, check_bundle, the loop over the
    snapshots (taking a snapshot and the pause between them do nothing), the end.
    Returns (exit code, stdout + stderr, event or None).
    """
    script = source('infra/lib/anysda-backup.sh')
    with tempfile.TemporaryDirectory() as directory:
        top = Path(directory)
        (top / 'etc').mkdir()
        (top / 'etc/tgbot-secret.txt').write_text('s3cret\n')
        (top / 'bundle').mkdir()
        root = bundle(top / 'bundle', text)
        fake_curl = ('curl() { local url=${*: -1}; [[ $url == ' + PANEL + ' ]] || return 0; '
                     'cat > header; for a in "$@"; do printf "%s\\n" "$a"; done > args; }\n')
        head = script.split('# Сверка среза с БД того же среза', 1)[0].replace('/etc/anysda/', f'{top}/etc/')
        head = head.replace('umask 077\n', 'umask 077\n' + fake_curl, 1)
        body = (head + cut(script, 'check_bundle() {', '\nPY\n}\n') + '\nPY\n}\n' +
                f'snapshot_access() {{ :; }}\nsleep() {{ :; }}\nBUNDLE_DIR={root}\n' +
                cut(script, 'for attempt in 1 2 3 4 5 6 7 8 9 10; do', '\ndone\n') + '\ndone\n' +
                script[script.rindex('if [[ -n $WARNING ]]; then'):])
        done = subprocess.run(['bash', '-c', body], cwd=top, capture_output=True, text=True, timeout=600,
                              env=os.environ | {'PATH': path, 'ANYSDA_LIB_DIR': str(LIB)}, check=False)
        args = (top / 'args').read_text().splitlines() if (top / 'args').exists() else None
    event = json.loads(args[args.index('-d') + 1]) if args else None
    return done.returncode, done.stdout + done.stderr, event


def shell_key(block, raw):
    """A shell block that writes server.priv from wg0.conf `raw`: what it writes."""
    with tempfile.TemporaryDirectory() as directory:
        top = Path(directory)
        (top / 'wg0.conf').write_bytes(raw)
        script = ('set -euo pipefail\nwork=' + directory + '\nWG_BUNDLE=' + directory + '\n' +
                  block.replace('/etc/wireguard/wg0.conf', str(top / 'wg0.conf')))
        done = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=60, check=False)
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
        for group in self.groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
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
            env = os.environ | {'PATH': path + os.pathsep + os.environ['PATH'], 'ANYSDA_LIB_DIR': str(LIB),
                                'PYTHONDONTWRITEBYTECODE': '1'}
            root = bundle(top / 'bundle', conf(BASE))
            (root / 'etc-wireguard/wg0.conf').write_bytes(mk())
            callers = {'backup --check-bundle': ['bash', str(LIB / 'anysda-backup.sh'), '--check-bundle', str(root)],
                       'verify apply_peers': [sys.executable, '-c', VERIFY, str(TESTS),
                                              str(root / 'etc-wireguard/wg0.conf')]}
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
                        peers = VT.wg_conf.conf_peers(mk())
                        self.assertEqual(sorted(peer['key'] for peer in peers), sorted([A.decode(), B.decode()]))
                        try:
                            self.assertEqual(holding_key(), [])
                            self.assertEqual(named() - self.before, set())
                        finally:
                            self.tearDown()

    def test_2_nul_as_wg_quick_and_wg(self):
        for name, raw in NUL_PEERS.items():
            error, _private, peers = oracle(raw)
            self.assertIsNone(error, name)
            for key, _, _ in peers:
                with self.subTest('restore: the peer wg holds is cut', edge=name, peer=key[:4]):
                    first, second = restore_cut(raw, {key})
                    self.assertEqual(second, first)
                    self.assertEqual(oracle(first)[2], [peer for peer in peers if peer[0] != key])
        restore = cut(source('infra/lib/anysda-restore.sh'), '  if [[ ! -s "$WG_BUNDLE/server.priv" ]]; then',
                      '\n  # pub выводим заново')
        stage = cut(source('infra/scripts/28-wireguard.sh'),
                    '  if [[ ! -s "$work/server.priv" && -s /etc/wireguard/wg0.conf ]]; then', '\n  # Сбой вычисления')
        for name, raw in NUL_PRIVATE.items():
            error, private, _peers = oracle(raw)
            self.assertEqual((error, private), (None, S.decode()), name)
            for who, block in (('restore', restore), ('28-wireguard', stage)):
                with self.subTest('server.priv is the key wg holds', edge=name, script=who):
                    self.assertEqual(shell_key(block, raw), private)

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
        start = time.monotonic()
        with self.subTest('verify apply_peers: ValueError naming wg0.conf'):
            try:
                VT.apply_peers(raw)
            except ValueError as error:
                self.assertIn('wg0.conf', str(error))
            except Exception as error:  # noqa: BLE001 - the check must not die of it
                self.fail(f'{type(error).__name__}: {error}')
            else:
                self.fail('accepted a file wg could not apply')
            self.assertLess(time.monotonic() - start, LIMIT + MARGIN)

    def test_4_nothing_to_check_with_is_not_a_mismatch(self):
        reasons = {'no-wg': 'нет wg', 'no-netns': 'Operation not permitted'}
        with tempfile.TemporaryDirectory() as directory:
            for kind, path in without_wg(directory).items():
                code, output, event = backup_run(conf(BASE), path)
                with self.subTest('backup: archive kept, the event says the check was not done', kind=kind):
                    self.assertNotEqual(code, 0, output)
                    self.assertIsNotNone(event, output)
                    self.assertIn('не выполнена', event['detail'])
                    self.assertIn(reasons[kind], event['detail'])
                    for word in ('расходятся', 'перевыпусти'):
                        self.assertNotIn(word, event['detail'])
                        self.assertNotIn(word, output)
                os.environ['PATH'], saved = path, os.environ['PATH']
                try:
                    _code, err = restore_check(conf(BASE))
                finally:
                    os.environ['PATH'] = saved
                with self.subTest('restore: a warning naming wg0.conf, no reissue', kind=kind):
                    self.assertIn('WARN', err)
                    self.assertIn('wg0.conf', err)
                    for word in ('расходятся', 'перевыпусти'):
                        self.assertNotIn(word, err)
        # A real mismatch as before: device 2 has no peer.
        code, output, event = backup_run(conf([BASE[0], BASE[1]]), os.environ['PATH'])
        with self.subTest('backup: a real mismatch keeps its text and code'):
            self.assertEqual(code, 4, output)
            self.assertEqual(event, {'type': 'backup_warning',
                                     'detail': 'БД и конфиги VPN расходятся (устройства: 2) — перевыпусти в панели'})
        _code, err = restore_check(conf([BASE[0], BASE[1]]))
        with self.subTest('restore: a real mismatch still says reissue'):
            self.assertIn('расходятся', err)
            self.assertIn('перевыпусти', err)


if __name__ == '__main__':
    unittest.main()
