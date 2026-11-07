#!/usr/bin/env python3
"""Run the discovered infra suite in parallel isolated processes.

Each module runs in its own process; PKI tests form their own partition, as
before. A class without class or module fixtures and without numbered tests is
cut into chunks of CHUNK tests. Modules sharing the WireGuard oracle key run one
after another in a single chain: one of them looks for that key in every netns
of the host. The summed test count must equal the discovered one, and any
failing partition fails the run.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent
CHUNK = 4
HOST_STATE = re.compile(r'test_wg_conf_wg_oracle|/run/netns')
# Slowest modules by measurement: only queue order, the next run's tail
SLOW = ('test_verify', 'test_ssh_limit_session', 'test_deploy_verify', 'test_verify_exit_ssh_limit_edge',
        'test_verify_exit_ssh_timeout', 'test_apply_openvpn_check_signal', 'test_apply_openvpn_check_phases',
        'test_ssh_probe_timeout_value')


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


def module_of(test):
    if isinstance(test, unittest.loader._FailedTest):
        return test._testMethodName
    return type(test).__module__


def group_of(test):
    return 'pki' if type(test).__name__ == 'PkiTests' else 'general'


def touches_host(module):
    source = HERE / f'{module}.py'
    return source.is_file() and bool(HOST_STATE.search(source.read_text(errors='replace')))


def divisible(test):
    """Tests of this class can run apart from each other."""
    cls = type(test)
    module = sys.modules.get(cls.__module__)
    return (group_of(test) == 'general' and not isinstance(test, unittest.loader._FailedTest)
            and 'setUpClass' not in vars(cls) and 'tearDownClass' not in vars(cls)
            and not hasattr(module, 'setUpModule') and not hasattr(module, 'tearDownModule')
            and not re.match(r'test_?\d', test._testMethodName))


def assign(tests):
    """(module, group, class, chunk) -> its tests, in discovery order."""
    parts, by_class = {}, {}
    for test in tests:
        if divisible(test):
            by_class.setdefault((module_of(test), type(test).__name__), []).append(test)
        else:
            parts.setdefault((module_of(test), group_of(test), None, None), []).append(test)
    for (module, cls), members in by_class.items():
        if len(members) <= CHUNK:
            parts.setdefault((module, 'general', None, None), []).extend(members)
            continue
        for chunk in range(0, len(members), CHUNK):
            parts[(module, 'general', cls, chunk // CHUNK)] = members[chunk:chunk + CHUNK]
    return parts


def run_partition(args):
    sys.path.insert(0, str(HERE))
    parts = assign(flatten(unittest.defaultTestLoader.loadTestsFromName(args.module)))
    selected = unittest.TestSuite(parts.get((args.module, args.group, args.cls, args.chunk), []))
    result = unittest.TextTestRunner(verbosity=2).run(selected)
    Path(args.report).write_text(str(result.testsRun))
    return int(not result.wasSuccessful() or not result.testsRun)


def name(key):
    module, group, cls, chunk = key
    return f'{module}[{group}]' if cls is None else f'{module}.{cls}[{chunk}]'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module')
    parser.add_argument('--group', choices=('pki', 'general'))
    parser.add_argument('--cls')
    parser.add_argument('--chunk', type=int)
    parser.add_argument('--report')
    parser.add_argument('--jobs', type=int, default=int(os.environ.get('INFRA_JOBS', 0)) or 2 * (os.cpu_count() or 1))
    args = parser.parse_args()
    if args.module:
        return run_partition(args)

    sys.path.insert(0, str(HERE))
    parts = {key: len(tests) for key, tests in assign(flatten(unittest.defaultTestLoader.discover(str(HERE)))).items()}
    expected = sum(parts.values())
    if not expected:
        return 1
    # PKI, the host chain and the slowest modules first, then the largest partitions
    host = [key for key in parts if key[1] == 'general' and touches_host(key[0])]
    rest = sorted((key for key in parts if key not in host),
                  key=lambda key: (key[1] != 'pki', key[0] not in SLOW, -parts[key]))
    chains = [[key] for key in rest if key[1] == 'pki'] + ([host] if host else [])
    chains += [[key] for key in rest if key[1] != 'pki']

    children = set()
    lock = threading.Lock()
    stopping = threading.Event()

    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)

    def run(key, directory):
        module, group, cls, chunk = key
        work = Path(tempfile.mkdtemp(dir=directory))
        log, report = work / 'output.log', work / 'count'
        command = [sys.executable, __file__, '--module', module, '--group', group, '--report', str(report)]
        if cls is not None:
            command += ['--cls', cls, '--chunk', str(chunk)]
        started = time.monotonic()
        with log.open('w') as output, lock:
            if stopping.is_set():
                return key, 1, 0, 0.0, ''
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            children.add(process)
        code = process.wait()
        with lock:
            children.discard(process)
        count = int(report.read_text()) if report.exists() else 0
        return key, code, count, time.monotonic() - started, log.read_text()

    def chain(keys, directory):
        return [run(key, directory) for key in keys]

    started = time.monotonic()
    results = []
    with tempfile.TemporaryDirectory(prefix='infra-parts-') as directory:
        pool = ThreadPoolExecutor(max_workers=max(1, args.jobs))
        try:
            for future in as_completed([pool.submit(chain, keys, directory) for keys in chains]):
                for key, code, count, seconds, output in future.result():
                    results.append((key, code, count, seconds))
                    print(f'===== {name(key)}: exit={code}, tests={count}, seconds={seconds:.1f}', flush=True)
                    print(output, end='', flush=True)
        finally:
            with lock:
                stopping.set()
                for process in children:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
            pool.shutdown(cancel_futures=True)

    ran = sum(count for _key, _code, count, _seconds in results)
    failed = [name(key) for key, code, _count, _seconds in results if code]
    print('slowest partitions:', ', '.join(
        f'{name(key)}={seconds:.0f}s' for key, _code, _count, seconds in sorted(results, key=lambda r: -r[3])[:10]))
    print(f'infra partitions: {len(results)} of {len(parts)}, jobs={args.jobs}, tests={ran} of {expected}, '
          f'seconds={time.monotonic() - started:.1f}, failed={failed}', flush=True)
    return int(bool(failed) or ran != expected or len(results) != len(parts))


if __name__ == '__main__':
    sys.exit(main())
