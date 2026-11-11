#!/usr/bin/env python3
"""Run the discovered suite (infra/tests, or --dir) in parallel isolated processes.

Each module runs in its own process. A class without class or module fixtures
and without numbered tests is cut into chunks of CHUNK tests, a class from
SINGLE or one whose part weighs LONG seconds or more into one test per part. Modules sharing the WireGuard oracle key run one
after another in a single chain: one of them looks for that key in every netns
of the host. Modules that compare elapsed time with a limit by a second or less
run last, one by one, with nothing else running. The pool takes the heaviest
chain first by the measured WEIGHT. A chain within CRITICAL of the part's
finish (the pool's even share or its longest chain) holds that finish: it runs
at normal priority, the rest under nice NICE. The summed test count must equal
the discovered one, and any failing partition fails the run.

--shard k/N runs one of N disjoint parts: quiet modules are spread first, then
each chain, heaviest first, goes to the part that would finish earliest with it
(its longest chain or its sum over the pool, plus its quiet tail), by WEIGHT
(else the test count). --manifest writes the discovered and the part's test
ids; --check-manifests checks that the parts of one run are disjoint and cover
everything discovered.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
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
# Seconds of a partition on GitHub, the slower variant of infra and telegram (refresh from the
# «===== name: ... seconds=» lines of a green run): queue order and --shard split only. Unlisted ones go
# after, larger first; a new slow module goes there or ends up in the run's tail.
# test_deploy_lock_orphan_session is left out: in the first wave its 15 s step overruns and fails the run
WEIGHT = json.loads((HERE / 'partition-weights.json').read_text())
# One test of these classes takes most of a CHUNK and holds the run's tail alone
SINGLE = ('test_verify.VerifyTest', 'test_deploy_verify.DeployVerifyTests', 'test_delivery.DeliveryProcessTests',
          'test_pki.PkiTests')
# Elapsed time against a limit with a margin of 0.1-2 s: parallel load is enough to cross it
# A divisible class this heavy holds the part's finish alone: one test per part (169 s of three
# swap tests in one process against 20-60 s each, run 42509594204)
LONG = 40
# Priority of chains off the critical path: on a 4-CPU runner the 192 s verify test ran 129 s with less beside it
NICE = 10
CRITICAL = 0.6
QUIET = ('test_verify_exit_ssh_limit_edge', 'test_verify_exit_ssh_remote_code', 'test_ssh_limit_remote_code',
         'test_release', 'test_probe_reads', 'test_udp_cut_switch_time', 'test_watchdog_package')


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


def touches_host(directory, module):
    source = directory / f'{module}.py'
    return source.is_file() and bool(HOST_STATE.search(source.read_text(errors='replace')))


def divisible(test):
    """Tests of this class can run apart from each other."""
    cls = type(test)
    module = sys.modules.get(cls.__module__)
    return (not isinstance(test, unittest.loader._FailedTest)
            and 'setUpClass' not in vars(cls) and 'tearDownClass' not in vars(cls)
            and not hasattr(module, 'setUpModule') and not hasattr(module, 'tearDownModule')
            and not re.match(r'test_?\d', test._testMethodName))


def heavy(module, cls, members):
    """The measured part a divisible class would run in weighs LONG seconds or more."""
    if len(members) <= CHUNK:
        return (WEIGHT.get(f'{module}[general]') or 0) >= LONG
    return any((WEIGHT.get(f'{module}.{cls}[{chunk}]') or 0) >= LONG for chunk in range(len(members) // CHUNK + 1))


def assign(tests):
    """(module, group, class, chunk) -> its tests, in discovery order."""
    parts, by_class = {}, {}
    for test in tests:
        if divisible(test):
            by_class.setdefault((module_of(test), type(test).__name__), []).append(test)
        else:
            parts.setdefault((module_of(test), 'general', None, None), []).append(test)
    for (module, cls), members in by_class.items():
        if f'{module}.{cls}' in SINGLE or (len(members) > 1 and heavy(module, cls, members)):
            share = (WEIGHT.get(f'{module}[general]') if len(members) <= CHUNK else None) or 0
            for test in members:
                SHARE[f'{module}.{cls}[{test._testMethodName}]'] = share / len(members)
                parts[(module, 'general', cls, test._testMethodName)] = [test]
            continue
        if len(members) <= CHUNK:
            parts.setdefault((module, 'general', None, None), []).extend(members)
            continue
        for chunk in range(0, len(members), CHUNK):
            parts[(module, 'general', cls, chunk // CHUNK)] = members[chunk:chunk + CHUNK]
    return parts


def run_partition(args):
    sys.path.insert(0, args.dir)
    parts = assign(flatten(unittest.defaultTestLoader.loadTestsFromName(args.module)))
    chunk = int(args.chunk) if args.chunk and args.chunk.isdigit() else args.chunk
    selected = unittest.TestSuite(parts.get((args.module, args.group, args.cls, chunk), []))
    result = unittest.TextTestRunner(verbosity=2).run(selected)
    Path(args.report).write_text(str(result.testsRun))
    return int(not result.wasSuccessful() or not result.testsRun)


def name(key):
    module, group, cls, chunk = key
    return f'{module}[{group}]' if cls is None else f'{module}.{cls}[{chunk}]'


# Unmeasured single test of a split class: its share of the class's measured part
SHARE = {}


def weight(key):
    return WEIGHT.get(name(key)) or SHARE.get(name(key)) or 0


def cost(keys, parts):
    return sum(weight(key) or parts[key] for key in keys)


def spread(groups, parts, count, jobs=1, tail=None):
    """Heaviest group to the part that would finish earliest with it; a part is a pool of jobs
    workers taking it on the least busy one, then its tail[i] seconds; ties go to the first part."""
    tail, shards = tail or [0] * count, [[] for _ in range(count)]
    workers = [[0] * jobs for _ in range(count)]

    def finish(index, seconds):
        busy = sorted(workers[index])
        busy[0] += seconds
        return max(busy) + tail[index]

    for keys in sorted(groups, key=lambda keys: -cost(keys, parts)):
        seconds = cost(keys, parts)
        index = min(range(count), key=lambda i: finish(i, seconds))
        busy = workers[index]
        busy[busy.index(min(busy))] += seconds
        shards[index].append(keys)
    return shards


def check_manifests(paths):
    manifests = [json.loads(Path(path).read_text()) for path in paths]
    total = {manifest['total'] for manifest in manifests}
    shards = sorted(manifest['shard'] for manifest in manifests)
    discovered = {tuple(manifest['discovered']) for manifest in manifests}
    problems = []
    count = next(iter(total)) if len(total) == 1 else 0
    if not count or shards != list(range(1, count + 1)):
        problems.append(f'parts {shards} of {sorted(total)}')
    if len(discovered) != 1:
        problems.append('parts discovered different suites')
    found = set(next(iter(discovered), ()))
    seen, overlap = set(), set()
    for manifest in manifests:
        mine = set(manifest['assigned'])
        overlap |= seen & mine
        seen |= mine
        if manifest['ran'] != len(mine):
            problems.append(f"part {manifest['shard']}: ran {manifest['ran']} of {len(mine)}")
    if overlap:
        problems.append(f'{len(overlap)} tests in more than one part: {sorted(overlap)[:5]}')
    if seen != found:
        problems.append(f'missing {sorted(found - seen)[:5]}, unknown {sorted(seen - found)[:5]}')
    print(f'infra parts: {len(manifests)}, tests={sum(m["ran"] for m in manifests)} of {len(found)}, '
          f'problems={problems}', flush=True)
    return int(bool(problems) or not found)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', default=str(HERE), help='directory of the suite')
    parser.add_argument('--module')
    parser.add_argument('--group', choices=('general',))
    parser.add_argument('--cls')
    parser.add_argument('--chunk')
    parser.add_argument('--report')
    parser.add_argument('--jobs', type=int, default=int(os.environ.get('INFRA_JOBS', 0)) or 2 * (os.cpu_count() or 1))
    parser.add_argument('--plan', action='store_true', help='print the queue with weights and exit')
    parser.add_argument('--shard', help='k/N: run part k of N')
    parser.add_argument('--manifest', help='write discovered and this part\'s test ids as JSON')
    parser.add_argument('--check-manifests', nargs='+', metavar='MANIFEST')
    args = parser.parse_args()
    if args.module:
        return run_partition(args)
    if args.check_manifests:
        return check_manifests(args.check_manifests)
    shard, total = (int(value) for value in (args.shard or '1/1').split('/'))
    if not 1 <= shard <= total:
        parser.error('--shard wants k/N with 1 <= k <= N')

    directory = Path(args.dir).resolve()
    args.dir = str(directory)
    sys.path.insert(0, args.dir)
    found = assign(flatten(unittest.defaultTestLoader.discover(args.dir)))
    parts = {key: len(tests) for key, tests in found.items()}
    # The heaviest chain first, unmeasured ones by size; quiet ones after the pool
    host = [key for key in parts if touches_host(directory, key[0])]
    quiet = [key for key in parts if key[0] in QUIET and key not in host]
    chains = [[key] for key in parts if key not in host and key not in quiet] + ([host] if host else [])
    # Quiet modules run alone after the pool: their sum is the tail each part's pool is balanced against
    tails = spread([[key] for key in quiet], parts, total)
    mine = [key for keys in tails[shard - 1] for key in keys]
    tail = [sum(cost(keys, parts) for keys in shard_tail) for shard_tail in tails]
    chains = spread(chains, parts, total, args.jobs, tail)[shard - 1]
    quiet = [key for key in quiet if key in mine]
    parts = {key: parts[key] for keys in chains + [quiet] for key in keys}
    expected = sum(parts.values())
    if not expected:
        return 1
    if args.manifest:
        Path(args.manifest).write_text(json.dumps({
            'shard': shard, 'total': total, 'ran': 0,
            'discovered': sorted(test.id() for tests in found.values() for test in tests),
            'assigned': sorted(test.id() for key in parts for test in found[key])}))
    chains.sort(key=lambda keys: (-sum(weight(key) for key in keys), -sum(parts[key] for key in keys)))
    # Critical: near the pool's finish (its even share or the longest chain), the rest yields the CPU to it
    finish = max([sum(cost(keys, parts) for keys in chains) / max(1, args.jobs)]
                 + [cost(keys, parts) for keys in chains])
    critical = {id(keys) for keys in chains if cost(keys, parts) >= CRITICAL * finish}
    if args.plan:
        for keys in chains + [quiet]:
            mark = '' if id(keys) in critical or keys is quiet else f' nice={NICE}'
            print(f'{sum(weight(key) for key in keys):g}{mark}', ' '.join(name(key) for key in keys))
        return 0

    children = set()
    lock = threading.Lock()
    stopping = threading.Event()

    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)

    def run(key, directory, nice=0):
        module, group, cls, chunk = key
        work = Path(tempfile.mkdtemp(dir=directory))
        log, report = work / 'output.log', work / 'count'
        command = [sys.executable, __file__, '--dir', args.dir, '--module', module, '--group', group,
                   '--report', str(report)]
        if cls is not None:
            command += ['--cls', cls, '--chunk', str(chunk)]
        started = time.monotonic()
        with log.open('w') as output, lock:
            if stopping.is_set():
                return key, 1, 0, 0.0, ''
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                       preexec_fn=(lambda: os.nice(nice)) if nice else None)
            children.add(process)
        code = process.wait()
        with lock:
            children.discard(process)
        count = int(report.read_text()) if report.exists() else 0
        return key, code, count, time.monotonic() - started, log.read_text()

    def chain(keys, directory, nice=0):
        return [run(key, directory, nice) for key in keys]

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
            for future in as_completed([pool.submit(chain, keys, directory, 0 if id(keys) in critical else NICE)
                                        for keys in chains]):
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
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text())
        manifest['ran'] = ran
        Path(args.manifest).write_text(json.dumps(manifest))
    print(f'infra partitions{"" if total == 1 else f" {shard}/{total}"}: {len(results)} of {len(parts)}, jobs={args.jobs}, tests={ran} of {expected}, '
          f'seconds={seconds:.1f}, cpu={usage.ru_utime + usage.ru_stime:.0f}s of {seconds * (os.cpu_count() or 1):.0f}s, '
          f'failed={failed}', flush=True)
    return int(bool(failed) or ran != expected or len(results) != len(parts))


if __name__ == '__main__':
    sys.exit(main())
