#!/usr/bin/env python3
"""Select panel e2e from a Git diff; unknown history always runs the suite."""

import argparse
import json
import os
from pathlib import Path
import subprocess


# Inputs read by run-local/panel-stand and the live speedtest regression.
DEPENDENCIES = {
    '.github/workflows/checks.yml',
    'infra/lib/e2e_paths.py',
    'infra/lib/entry_resources.py',
    'infra/scripts/29-openvpn.sh',
    'infra/scripts/30-frontend.sh',
    'infra/tools/speedtest.py',
    'infra/tests/test_speedtest.py',
    'infra/tests/test_deploy.py',
    'infra/tests/test_panel_proxy.py',
    'infra/lib/config2env.py',
    'infra/lib/hy2_bandwidth.py',
    'infra/scripts/20-ru-router.sh',
    'infra/lib/gen-router-config.py',
    'infra/tests/router_support.py',
    # app/e2e/qr-size.sh: QR аварийки hy2 и худший конфиг для бота.
    'infra/emergency/vpn2_emergency/hy2.py',
    'infra/emergency/vpn2_emergency/web.py',
    'infra/tests/emergency_qr_fixture.py',
    'telegram/tests/qr-worst-wg.conf',
    'telegram/tests/test_qr_size.py',
}


def needs_e2e(paths):
    return any(path.startswith('app/') or path in DEPENDENCIES for path in paths)


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
