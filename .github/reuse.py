#!/usr/bin/env python3
"""Reuse a finished run of the same commit instead of repeating it on dev, main and the release tag.

checks: any earlier push run of the commit that ran the checks green; e2e is reused only if it ran there.
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


def checks():
    """Only earlier runs are waited for: two runs of one commit never wait for each other."""
    deadline = time.monotonic() + WAIT
    while True:
        earlier = [r for r in runs('checks.yml') if r['id'] < RUN]
        rest = e2e = None
        for run in (r for r in earlier if r['conclusion'] == 'success'):
            done = jobs(run)
            if any(ran(j, 'pytest') for j in done if j['name'].startswith('пакет vpn2')):
                rest = rest or run
                if any(ran(j, 'сквозные тесты') for j in done):
                    e2e = e2e or run
        if rest or not any(r['status'] != 'completed' for r in earlier) or time.monotonic() > deadline:
            break
        time.sleep(20)
    if rest:
        print(f"::notice title=checks::проверки этого коммита уже прошли в {rest['html_url']}")
        yield 'rest', 'skip'
    if e2e:
        yield 'e2e', 'skip'


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
        outputs = list({'checks': checks, 'images': images}[sys.argv[1]]())
    except (subprocess.CalledProcessError, KeyError, ValueError) as error:
        print(f'::warning::повтор не проверен, прогон полный: {error}')
        outputs = []
    with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
        out.writelines(f'{k}={v}\n' for k, v in outputs)
    print(dict(outputs) or 'full run')
