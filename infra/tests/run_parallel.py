#!/usr/bin/env python3
"""Run the discovered infra suite in parallel isolated processes.

Each module runs in its own process; PKI tests form their own partition, as
before. A class without class or module fixtures and without numbered tests is
cut into chunks of CHUNK tests, a class from SINGLE into one test per part. Modules sharing the WireGuard oracle key run one
after another in a single chain: one of them looks for that key in every netns
of the host. Modules that compare elapsed time with a limit by a second or less
run last, one by one, with nothing else running. The pool takes the heaviest
chain first by the measured WEIGHT. The summed test count must equal the
discovered one, and any failing partition fails the run.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import re
import resource
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
# Seconds of a partition in infra (true) on GitHub (dev cdfd3ce): queue order only. Unlisted ones
# go after, larger first; a new slow module goes here or ends up in the run's tail
WEIGHT = {
    'test_verify.VerifyTest[test_individual_entry_failures]': 245,
    'test_verify.VerifyTest[test_fresh_scrape_after_tunnel_restart_ten_times]': 50,
    'test_pki[pki]': 239, 'test_deploy_verify[general]': 136, 'test_ssh_limit_session[general]': 128,
    'test_wg_client_lane_route[general]': 112, 'test_ssh_limit_slow_runner[general]': 91,
    'test_wg_conf_wg_oracle[general]': 88, 'test_bootstrap_swap_container[general]': 86,
    'test_wg_conf_kill_nul_nocheck[general]': 74,
    'test_verify_exit_ssh_timeout[general]': 67, 'test_ssh_dd_notrunc[general]': 66,
    'test_apply_openvpn_check_signal[general]': 66, 'test_ssh_probe_timeout_value[general]': 61,
    'test_apply_openvpn_check_phases[general]': 60, 'test_telegram_from_config.TelegramFromConfigTests[0]': 54,
    'test_apply_openvpn_check_env.OpenvpnCheckRefusedTests[0]': 52, 'test_fw_guard_entry_slow[general]': 49,
    'test_fw_guard_late_beat[general]': 48, 'test_verify_traffic[general]': 47,
    'test_panel_closed_journal[general]': 46, 'test_verify_exit_target[general]': 40,
    'test_stage_exit_login_hang[general]': 39, 'test_deploy_lock_orphan_session[general]': 38,
    'test_adguard_panel_settings[general]': 32, 'test_emergency_agent.EmergencyAgentTests[11]': 29,
    'test_verify_exit_ssh_bashrc[general]': 27, 'test_telegram_bot_off.TelegramConfigRemovedTests[0]': 25,
    'test_hy2_isolation[general]': 23, 'test_emergency_hy2.EmergencyHy2Tests[3]': 23,
    'test_verify_result[general]': 22, 'test_verify_exit_ssh_stderr[general]': 22,
    'test_hy2hop_disable_unit[general]': 22, 'test_emergency_memory[general]': 22, 'test_telegram_stage[general]': 21,
    'test_hy2hop_enable_starts_unit[general]': 21,
}
# One test of these classes takes most of a CHUNK and holds the run's tail alone
SINGLE = ('test_verify.VerifyTest',)
# Elapsed time against a limit with a margin of 0.1-2 s: parallel load is enough to cross it
QUIET = ('test_verify_exit_ssh_limit_edge', 'test_verify_exit_ssh_remote_code', 'test_ssh_limit_remote_code',
         'test_control', 'test_release', 'test_probe_reads')


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
        if f'{module}.{cls}' in SINGLE:
            for test in members:
                parts[(module, 'general', cls, test._testMethodName)] = [test]
            continue
        if len(members) <= CHUNK:
            parts.setdefault((module, 'general', None, None), []).extend(members)
            continue
        for chunk in range(0, len(members), CHUNK):
            parts[(module, 'general', cls, chunk // CHUNK)] = members[chunk:chunk + CHUNK]
    return parts


def run_partition(args):
    sys.path.insert(0, str(HERE))
    parts = assign(flatten(unittest.defaultTestLoader.loadTestsFromName(args.module)))
    chunk = int(args.chunk) if args.chunk and args.chunk.isdigit() else args.chunk
    selected = unittest.TestSuite(parts.get((args.module, args.group, args.cls, chunk), []))
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
    parser.add_argument('--chunk')
    parser.add_argument('--report')
    parser.add_argument('--jobs', type=int, default=int(os.environ.get('INFRA_JOBS', 0)) or 2 * (os.cpu_count() or 1))
    parser.add_argument('--plan', action='store_true', help='print the queue with weights and exit')
    args = parser.parse_args()
    if args.module:
        return run_partition(args)

    sys.path.insert(0, str(HERE))
    parts = {key: len(tests) for key, tests in assign(flatten(unittest.defaultTestLoader.discover(str(HERE)))).items()}
    expected = sum(parts.values())
    if not expected:
        return 1
    # The heaviest chain first, unmeasured ones by size; quiet ones after the pool
    host = [key for key in parts if key[1] == 'general' and touches_host(key[0])]
    quiet = [key for key in parts if key[0] in QUIET and key not in host]
    chains = [[key] for key in parts if key not in host and key not in quiet] + ([host] if host else [])
    chains.sort(key=lambda keys: (-sum(WEIGHT.get(name(key), 0) for key in keys), -sum(parts[key] for key in keys)))
    if args.plan:
        for keys in chains + [quiet]:
            print(sum(WEIGHT.get(name(key), 0) for key in keys), ' '.join(name(key) for key in keys))
        return 0

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

    def report(batch):
        for key, code, count, seconds, output in batch:
            results.append((key, code, count, seconds))
            print(f'===== {name(key)}: exit={code}, tests={count}, seconds={seconds:.1f}', flush=True)
            print(output, end='', flush=True)

    with tempfile.TemporaryDirectory(prefix='infra-parts-') as directory:
        pool = ThreadPoolExecutor(max_workers=max(1, args.jobs))
        try:
            for future in as_completed([pool.submit(chain, keys, directory) for keys in chains]):
                report(future.result())
            report(chain(quiet, directory))
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
    seconds = time.monotonic() - started
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(f'infra partitions: {len(results)} of {len(parts)}, jobs={args.jobs}, tests={ran} of {expected}, '
          f'seconds={seconds:.1f}, cpu={usage.ru_utime + usage.ru_stime:.0f}s of {seconds * (os.cpu_count() or 1):.0f}s, '
          f'failed={failed}', flush=True)
    return int(bool(failed) or ran != expected or len(results) != len(parts))


if __name__ == '__main__':
    sys.exit(main())
