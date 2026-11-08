#!/usr/bin/env python3
"""Run Playwright shards against separate panels, then merge their reports."""

import os
import json
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time


def assign_files(files, count, weights):
    """Measured seconds only balance work; every discovered spec runs once."""
    groups = [[] for _ in range(count)]
    loads = [0.0] * count
    for file in sorted(files, key=lambda f: (-weights.get(f, 10), f)):
        index = min(range(count), key=lambda i: (loads[i], i))
        groups[index].append(file)
        loads[index] += weights.get(file, 10)
    return groups


def part_groups(units, part, count, weights):
    """CI part "k/n": one weighted split over all n*count panels, part k keeps its slice.

    The slices partition the units: every test is in exactly one part.
    """
    index, total = (int(x) for x in part.split('/'))
    if not 1 <= index <= total:
        sys.exit(f'E2E_PART must be k/n with 1 <= k <= n, not {part}')
    groups = assign_files(units, total * count, weights)
    flat = sorted(unit for group in groups for unit in group)
    if flat != sorted(units):
        sys.exit('weighted split lost or duplicated a test')
    sizes = [len(group) for group in groups]
    print(f'units: {len(units)}, per panel {min(sizes)}..{max(sizes)}', flush=True)
    return groups[(index - 1) * count:index * count]


def listed_tests(report):
    """(file, line) for every test of a Playwright JSON report or --list.

    The line is the --test-list form: "[project] › file › describe › title".
    """
    tests = []

    def walk(suites, titles):
        for suite in suites:
            # Верхний уровень - сам файл, его заголовок в строку не входит.
            path = [] if titles is None else [*titles, suite['title']]
            for spec in suite.get('specs', []):
                for test in spec['tests']:
                    tests.append((spec['file'], ' › '.join(
                        [f"[{test['projectName']}]", spec['file'], *path, spec['title']])))
            walk(suite.get('suites', []), path)

    walk(report.get('suites', []), None)
    return tests


def split_units(files, tests, weights):
    """A file with per-test weights is split into its tests, the rest stay whole.

    Only files measured test by test in shard-weights.json split: their tests
    keep no state for one another.
    """
    units, unit_weights = [], {}
    for file in files:
        lines = [line for owner, line in tests if owner == file]
        if lines and all(line in weights for line in lines):
            units += lines
            unit_weights.update((line, weights[line]) for line in lines)
        else:
            units.append(file)
            unit_weights[file] = weights.get(file, 10)
    return units, unit_weights


def panel_tests(group, tests):
    """Test lines one panel runs: whole files of the group plus its split-off tests."""
    units = set(group)
    return [line for file, line in tests if file in units or line in units]


def panel_arguments(group, tests, report):
    """Playwright filters: the panel's files; tests of split files run elsewhere are inverted out."""
    mine = set(panel_tests(group, tests))
    owners = sorted({file for file, line in tests if line in mine} | {unit for unit in group if '[' not in unit})
    elsewhere = [line for file, line in tests if file in owners and line not in mine]
    arguments = [f'e2e/{re.escape(file)}$' for file in owners]
    if elsewhere:
        invert = report / 'elsewhere.txt'
        invert.write_text(''.join(line + '\n' for line in elsewhere))
        arguments += ['--test-list-invert', str(invert)]
    return arguments


def main():
    app = Path(__file__).resolve().parent.parent
    count = int(os.environ.get("E2E_SHARDS", "4"))
    port = int(os.environ.get("E2E_PORT", "31710"))
    browser_port = int(os.environ.get("E2E_BROWSER_PORT", str(port + 1)))
    name = os.environ.get("E2E_CONTAINER", "vpn2-panel-e2e")
    docker = shlex.split(os.environ.get("DOCKER", "docker"))
    if not 2 <= count <= 16:
        sys.exit("E2E_SHARDS must be between 2 and 16 (or 1 for a serial run)")
    ports = [p + 2 * i for i in range(count) for p in (port, browser_port)]
    if len(set(ports)) != len(ports) or not all(0 < p < 65536 for p in ports):
        sys.exit("E2E_PORT and E2E_BROWSER_PORT must leave distinct valid ports for every shard")
    processes = []
    groups = None
    part = os.environ.get('E2E_PART')
    tests = []
    if len(sys.argv) == 1:
        # Ask Playwright: filesystem globs could omit future .test.ts/.spec.js
        # files or disagree with project-specific testMatch/testIgnore rules.
        listing = json.loads(subprocess.check_output(
            [str(app / 'node_modules/.bin/playwright'), 'test', '--list', '--reporter=json'],
            cwd=app, text=True,
            env={**{key: value for key, value in os.environ.items() if key != 'PLAYWRIGHT_JSON_OUTPUT_FILE'},
                 'E2E_ADMIN_PASSWORD': 'listing-only'},
        ))
        files = sorted({suite['file'] for suite in listing['suites']})
        tests = listed_tests(listing)
        units, weights = split_units(files, tests, json.loads((app / 'e2e/shard-weights.json').read_text()))
        groups = part_groups(units, part, count, weights) if part else assign_files(units, count, weights)
        if any(not group for group in groups):
            sys.exit('Every shard must contain at least one spec')

    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    results = app / "test-results"
    results.mkdir(exist_ok=True)
    started = time.monotonic()
    # A fresh directory prevents stale reports from a previous failed run
    # being included in the count. Keep logs/traces even when a shard fails.
    with tempfile.TemporaryDirectory(prefix="shards-", dir=results) as blobs:
        try:
            for index in range(1, count + 1):
                shard_name = f"{name}-shard-{index}"
                report = results / f"shard-{index}"
                report.mkdir(exist_ok=True)
                env = {
                    **os.environ,
                    "E2E_SHARD": f"{index}/{count}",
                    "E2E_CONTAINER": shard_name,
                    "E2E_PORT": str(port + 2 * (index - 1)),
                    "E2E_BROWSER_PORT": str(browser_port + 2 * (index - 1)),
                    "E2E_REPORT_DIR": str(report),
                    "PLAYWRIGHT_BLOB_OUTPUT_FILE": str(Path(blobs) / f"report-{index}.zip"),
                }
                with (report / "run.log").open("w") as log:
                    arguments = (panel_arguments(groups[index - 1], tests, report)
                                 if groups else [*sys.argv[1:], '--shard', f'{index}/{count}'])
                    command = subprocess.Popen(
                        ["bash", "e2e/run-local.sh", *arguments, "--reporter=list,blob"],
                        cwd=app, env=env, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                processes.append((command, shard_name, report))
                print(f"shard {index}/{count}: started", flush=True)
            codes = []
            for command, shard_name, report in processes:
                codes.append(command.wait())
                print(f"--- {shard_name}: exit={codes[-1]} ---", flush=True)
                print((report / "run.log").read_text(), flush=True)
            merged = subprocess.run(
                [str(app / "node_modules/.bin/playwright"), "merge-reports",
                 "--reporter=html,json", blobs], cwd=app,
                env={**os.environ, "PLAYWRIGHT_JSON_OUTPUT_FILE": str(results / "results.json"),
                     "PLAYWRIGHT_HTML_OPEN": "never"},
                check=False,
            )
            complete = len(list(Path(blobs).glob("*.zip"))) == count
            if part and groups:
                # Сверка: панели этой части прогнали ровно свои тесты. Части
                # делят одно разбиение (part_groups), поэтому все части вместе -
                # каждый тест набора ровно один раз.
                planned = sorted(line for group in groups for line in panel_tests(group, tests))
                report = results / "results.json"
                ran = sorted(line for _, line in listed_tests(json.loads(report.read_text()))) if report.exists() else []
                print(f"tests: part {part} planned {len(planned)}, ran {len(ran)}, "
                      f"suite {len(tests)} in {len(files)} spec", flush=True)
                if ran != planned:
                    print(f"missing={sorted(set(planned) - set(ran))} extra={sorted(set(ran) - set(planned))}",
                          flush=True)
                    complete = False
            print(f"shards: count={count}, seconds={time.monotonic() - started:.1f}, "
                  f"exit_codes={codes}, reports_complete={complete}", flush=True)
            return int(any(codes) or merged.returncode != 0 or not complete)
        finally:
            for command, shard_name, _report in processes:
                if command.poll() is None:
                    os.killpg(command.pid, signal.SIGTERM)
                    try:
                        command.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(command.pid, signal.SIGKILL)
                        command.wait()
                # Also covers cancellation during container startup.
                subprocess.run([*docker, "rm", "-f", shard_name, f"{shard_name}-caddy",
                                f"{shard_name}-browser"], capture_output=True, check=False)
                subprocess.run([*docker, "network", "rm", shard_name], capture_output=True, check=False)
                subprocess.run([*docker, "volume", "rm", f"{shard_name}-data"], capture_output=True, check=False)


if __name__ == "__main__":
    sys.exit(main())
