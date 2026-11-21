#!/usr/bin/env python3
"""Select panel e2e from a Git diff; unknown history always runs the suite."""

import argparse
import json
import os
from pathlib import Path
import subprocess


# Inputs of app/e2e: copied into the stand, run or imported by it (test_e2e_paths derives them).
PREFIXES = (
    'app/',
    'infra/vpn2/src/vpn2/',
    'infra/ansible/roles/control/templates/',
    'telegram/tests/',
    'telegram/vpn2bot/',
)
DEPENDENCIES = {
    '.github/workflows/checks.yml',
    'infra/tests/e2e_paths.py',
    'infra/ansible/inventory.py',
    'infra/ansible/constraints.txt',
    'infra/ansible/roles/ikev2/files/anysda-ikev2-sync.py',
    # The router role run by infra/tests/router_support.py for the stand's route snapshot.
    'infra/ansible/roles/router/defaults/main.yml',
    'infra/ansible/roles/router/templates/config-base.json.j2',
    'infra/tests/router_support.py',
    'infra/tests/vpn_roles.py',
    'infra/tests/infra_support.py',
    'infra/tests/speedtest.py',
    'infra/tests/test_speedtest.py',
    'infra/tests/test_panel_proxy.py',
    'infra/tests/emergency_qr_fixture.py',
    'infra/tests/emergency_web_fixture.py',
    'telegram/requirements.txt',
}


def needs_e2e(paths):
    return any(path.startswith(PREFIXES) or path in DEPENDENCIES for path in paths)


def changed_paths(base, head, merge_base=False):
    # --no-renames includes both names: moving a file out of app also matters.
    diff = subprocess.run(
        ['git', 'diff', '--name-only', '--no-renames', '-z',
         f'{base}...{head}' if merge_base else base, *([] if merge_base else [head]), '--'],
        capture_output=True, check=True,
    )
    return diff.stdout.decode('utf-8', errors='surrogateescape').rstrip('\0').split('\0')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='origin/dev')
    parser.add_argument('--head', default='HEAD')
    parser.add_argument('--github', action='store_true')
    args = parser.parse_args()
    base, head, merge_base = args.base, args.head, True
    try:
        if args.github:
            event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
            if os.environ['GITHUB_EVENT_NAME'] == 'pull_request':
                base = event['pull_request']['base']['sha']
                head = event['pull_request']['head']['sha']
            elif os.environ['GITHUB_EVENT_NAME'] == 'push':
                base, head, merge_base = event['before'], event['after'], False
            else:
                raise ValueError('Unknown event')
        selected = needs_e2e(changed_paths(base, head, merge_base))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        selected = True
    print(str(selected).lower())


if __name__ == '__main__':
    main()
