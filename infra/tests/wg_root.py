"""wg0.conf tests that ask wg itself: a fresh wg0 in a netns needs root (CAP_NET_ADMIN).

`@as_root` on a TestCase: as root the class runs as it is; otherwise the class runs
again under `sudo -n` (the same file, only this class) and reports that run; without
`sudo -n` it is skipped with the reason.
"""
import os
import subprocess
import sys
import unittest

REASON = 'needs root or sudo -n: wg setconf on a fresh wg0 in a netns'


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
