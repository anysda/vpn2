"""wg0.conf tests that ask wg itself: a fresh wg0 in a netns needs root (CAP_NET_ADMIN).

`@as_root` on a TestCase: as root the class runs as it is; otherwise the class runs
again under `sudo -n` (the same file, only this class) and reports that run; without
`sudo -n` it is skipped with the reason.

`oracle` is wg: a copy of wg-quick strips the file (bash `read` drops NUL), the real
`wg setconf wg0 /dev/stdin` applies the rest to a fresh wg0 in a temporary netns,
`wg show wg0 dump` gives the peers. /usr/bin/wg and wg-quick do not read /tmp under
AppArmor: the body goes through stdin, wg-quick runs as a copy.
"""
import itertools
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REASON = 'needs root or sudo -n: wg setconf on a fresh wg0 in a netns'
WG, WG_QUICK, IP = shutil.which('wg'), shutil.which('wg-quick'), shutil.which('ip')


def as_root(cls):
    if os.geteuid() == 0:
        return cls
    if subprocess.run(['sudo', '-n', 'true'], capture_output=True, check=False).returncode:
        return unittest.skip(REASON)(cls)
    path = os.path.abspath(sys.modules[cls.__module__].__file__)
    keep = [f'{name}={os.environ[name]}' for name in ('VPN2_TEST_ROOT', 'PATH') if name in os.environ]

    def test_as_root(self):
        done = subprocess.run(['sudo', '-n', 'env', 'PYTHONDONTWRITEBYTECODE=1', *keep, sys.executable, path,
                               cls.__name__], capture_output=True, text=True, timeout=3600, check=False)
        sys.stderr.write(done.stderr[-2000:])
        self.assertEqual(done.returncode, 0, done.stdout[-4000:] + done.stderr[-8000:])

    return type(cls.__name__, (unittest.TestCase,), {'__doc__': cls.__doc__, '__module__': cls.__module__,
                                                     'test_as_root': test_as_root})


_names = itertools.count()


def ns_wg(body, *show):
    """`wg setconf wg0 /dev/stdin` of `body` on a fresh wg0 in a temporary netns: (error or None, dump lines)."""
    name = f'vpn2t12o{os.getpid()}x{next(_names)}'
    subprocess.run([IP, 'netns', 'add', name], check=True, capture_output=True)
    try:
        subprocess.run([IP, '-n', name, 'link', 'add', 'wg0', 'type', 'wireguard'], check=True, capture_output=True)
        done = subprocess.run([IP, 'netns', 'exec', name, WG, 'setconf', 'wg0', '/dev/stdin'], input=body,
                              capture_output=True, check=False)
        if done.returncode:
            return done.stderr.decode(errors='replace').strip() or 'rejected', []
        dump = subprocess.run([IP, 'netns', 'exec', name, WG, 'show', 'wg0', *(show or ('dump',))], check=True,
                              capture_output=True).stdout.decode()
        return None, dump.splitlines()
    finally:
        subprocess.run([IP, 'netns', 'del', name], check=False, capture_output=True)


def oracle(raw):
    """wg-quick strip (a copy of the script) + wg: (error or None, private key, sorted [(key, PSK, AllowedIPs)])."""
    with tempfile.TemporaryDirectory() as directory:
        top = Path(directory)
        shutil.copy(WG_QUICK, top / 'wg-quick')
        (top / 'wg0.conf').write_bytes(raw)
        (top / 'wg0.conf').chmod(0o600)
        strip = subprocess.run(['bash', str(top / 'wg-quick'), 'strip', str(top / 'wg0.conf')],
                               capture_output=True, check=False)
    if strip.returncode:
        return 'wg-quick: ' + strip.stderr.decode(errors='replace').strip(), None, None
    error, dump = ns_wg(strip.stdout)
    if error:
        return error, None, None
    peers = []
    for line in dump[1:]:
        key, psk, _endpoint, ips_ = line.split('\t')[:4]
        peers.append((key, '' if psk == '(none)' else psk, sorted([] if ips_ == '(none)' else ips_.split(','))))
    return None, dump[0].split('\t')[0], sorted(peers)
