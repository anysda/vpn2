#!/usr/bin/env python3
"""Run the same discovered infra suite with isolated PKI fixtures in parallel."""

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', choices=('pki', 'general'))
    args = parser.parse_args()
    if args.group:
        suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent))
        selected = unittest.TestSuite(test for test in flatten(suite)
                                     if (type(test).__name__ == 'PkiTests') == (args.group == 'pki'))
        if not selected.countTestCases():
            return 1
        return int(not unittest.TextTestRunner(verbosity=2).run(selected).wasSuccessful())

    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    started = time.monotonic()
    children = []
    codes = []
    with tempfile.TemporaryDirectory(prefix='infra-groups-') as directory:
        try:
            for group in ('pki', 'general'):
                log = Path(directory) / f'{group}.log'
                with log.open('w') as output:
                    process = subprocess.Popen([sys.executable, __file__, '--group', group],
                                               stdout=output, stderr=subprocess.STDOUT,
                                               start_new_session=True)
                children.append((process, log))
            for process, log in children:
                codes.append(process.wait())
                print(log.read_text(), end='', flush=True)
        finally:
            for process, _log in children:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait()
    print(f'infra groups: seconds={time.monotonic() - started:.1f}, exit_codes={codes}', flush=True)
    return int(any(codes))


if __name__ == '__main__':
    sys.exit(main())
