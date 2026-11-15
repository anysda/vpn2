#!/usr/bin/env python3
"""Reuse a finished run of the same commit instead of repeating it on dev, main and the release tag.

checks: any earlier push run of the commit that ran the checks green; e2e is reused only if it ran there.
stand: an earlier push run of the commit (lane/release before dev).
images: the dev run of the commit; its images are retagged, not rebuilt.
Prints step outputs; none means "run everything here". Any doubt falls back to a full run.
"""
import json
import os
import subprocess
import sys
import time

REPO, SHA, RUN = os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_SHA'], int(os.environ['GITHUB_RUN_ID'])
WAIT = 25 * 60


def api(path):
    return json.loads(subprocess.check_output(['gh', 'api', f'repos/{REPO}/{path}']))


def runs(workflow, branch=''):
    query = f'head_sha={SHA}&event=push&per_page=100' + (f'&branch={branch}' if branch else '')
    return [r for r in api(f'actions/workflows/{workflow}/runs?{query}')['workflow_runs'] if r['id'] != RUN]


def jobs(run):
    return api(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100")['jobs']


def ran(job, step_prefix):
    return job['conclusion'] == 'success' and any(
        s['name'].startswith(step_prefix) and s['conclusion'] == 'success' for s in job['steps'])


def earlier_green(workflow, job_prefix, step_prefix):
    """Green earlier runs of the commit that did the work, with their jobs. Only earlier runs
    are waited for: two runs of one commit never wait for each other."""
    deadline = time.monotonic() + WAIT
    while True:
        earlier = [r for r in runs(workflow) if r['id'] < RUN]
        if all(r['status'] == 'completed' for r in earlier) or time.monotonic() > deadline:
            break
        time.sleep(20)
    for run in (r for r in earlier if r['conclusion'] == 'success'):
        done = jobs(run)
        if any(ran(j, step_prefix) for j in done if j['name'].startswith(job_prefix)):
            yield run, done


def checks():
    green = list(earlier_green('checks.yml', 'пакет vpn2', 'pytest'))
    if green:
        print(f"::notice title=checks::проверки этого коммита уже прошли в {green[0][0]['html_url']}")
        yield 'rest', 'skip'
    if any(ran(j, 'сквозные тесты') for _, done in green for j in done):
        yield 'e2e', 'skip'


def stand():
    """Push runs only: a manual run may carry a negative control instead of the real check."""
    for run, _ in earlier_green('stand.yml', 'стенд развёртывание', 'deploy'):
        print(f"::notice title=stand::стенд этого коммита уже пройден в {run['html_url']}")
        yield 'run', 'skip'
        return


def images():
    """main and the tag point at the dev images: the release manifest names them by digest."""
    start = time.monotonic()
    while True:
        dev, waited = runs('images.yml', 'dev'), time.monotonic() - start
        # A push of dev and the tag together: the dev run may show up a little later.
        if not dev and (api('commits/dev')['sha'] != SHA or waited > 180) or waited > WAIT:
            return
        if dev and all(r['status'] == 'completed' for r in dev):
            break
        time.sleep(20)
    for run in (r for r in dev if r['conclusion'] == 'success'):
        built = [j for j in jobs(run) if j['name'].startswith('образ ')]
        if len(built) == 3 and all(ran(j, 'push') for j in built):
            print(f"::notice title=images::образы этого коммита собраны в {run['html_url']}")
            yield 'build', 'skip'
            return


if __name__ == '__main__':
    try:
        outputs = list({'checks': checks, 'stand': stand, 'images': images}[sys.argv[1]]())
    except (subprocess.CalledProcessError, KeyError, ValueError) as error:
        print(f'::warning::повтор не проверен, прогон полный: {error}')
        outputs = []
    with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
        out.writelines(f'{k}={v}\n' for k, v in outputs)
    print(dict(outputs) or 'full run')
