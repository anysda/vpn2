"""Build the published contract from this checkout and pushed image digests.

Every pinned version and hash comes from infra/versions.yml, nowhere else.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import DOWNLOAD_PAUSES, GITHUB_API, ReleaseError, command, digest, sha256, source_files, versions


def fetch(url, timeout=60):
    """GET url with retries; the workflow token lifts the anonymous API limit of a shared runner."""
    headers = {}
    token = os.environ.get('GITHUB_TOKEN')
    if token and url.startswith(GITHUB_API + '/'):
        headers['Authorization'] = f'Bearer {token}'
    attempts = len(DOWNLOAD_PAUSES) + 1
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            error = f'HTTP {exc.code} {exc.reason}'
            # 403 is the GitHub API rate limit; other 4xx will not change on retry.
            if exc.code < 500 and exc.code not in (403, 429):
                break
        except OSError as exc:
            error = str(getattr(exc, 'reason', '') or exc) or type(exc).__name__
        if attempt < attempts:
            pause = DOWNLOAD_PAUSES[attempt - 1]
            print(f'build-release: {url}: {error} ({attempt}/{attempts}), retry in {pause} s', file=sys.stderr)
            time.sleep(pause)
    raise ReleaseError(f'{url}: {error}')


def binary_specs(root):
    pins = versions(root / 'infra/versions.yml')
    version = pins['sing_box']
    specs = {}
    for arch in ('amd64', 'arm64'):
        specs['sing-box-' + arch] = {
            'version': version, 'architecture': arch,
            'sha256': pins['sing_box_sha256_' + arch],
            'url': f'https://github.com/SagerNet/sing-box/releases/download/v{version}/sing-box-{version}-linux-{arch}.tar.gz',
        }
    entries = (
        ('wgcf', 'wgcf', 'ViRb3/wgcf', 'v{v}', 'wgcf_{v}_linux_amd64'),
        ('caddy', 'caddy', 'caddyserver/caddy', 'v{v}', 'caddy_{v}_linux_amd64.deb'),
        ('node-exporter', 'node_exporter', 'prometheus/node_exporter', 'v{v}', 'node_exporter-{v}.linux-amd64.tar.gz'),
        ('adguard', 'adguard', 'AdguardTeam/AdGuardHome', '{v}', 'AdGuardHome_linux_amd64.tar.gz'),
    )
    for name, pin, repo, tag, asset in entries:
        version = pins[pin]
        filename = asset.format(v=version)
        url = f'https://api.github.com/repos/{repo}/releases/tags/{tag.format(v=version)}'
        release = json.loads(fetch(url))
        asset_data = next(a for a in release['assets'] if a['name'] == filename)
        specs[name] = {'version': version, 'architecture': 'amd64',
                       'url': asset_data['browser_download_url'],
                       'sha256': digest(asset_data['digest']).split(':')[1]}
    version = pins['yt_node']
    specs['yt-node'] = {'version': version, 'architecture': 'amd64',
                        'url': f'https://nodejs.org/dist/{version}/node-{version}-linux-x64.tar.xz',
                        'sha256': pins['yt_node_sha256']}
    version = pins['yt_pot']
    specs['yt-pot'] = {'version': version, 'architecture': 'all',
                       'url': f'https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/{version}.tar.gz',
                       'sha256': pins['yt_pot_sha256']}
    version = pins['aws_cli']
    url = f'https://awscli.amazonaws.com/awscli-exe-linux-x86_64-{version}.zip'
    checksum = hashlib.sha256(fetch(url, timeout=120)).hexdigest()
    specs['aws-cli'] = {'version': version, 'architecture': 'amd64', 'url': url, 'sha256': checksum}
    signature = hashlib.sha256(fetch(url + '.sig')).hexdigest()
    specs['aws-cli-signature'] = {'version': version, 'architecture': 'amd64', 'url': url + '.sig', 'sha256': signature}
    version = pins['zapret2']
    url = f'https://github.com/bol-van/zapret2/archive/{version}.tar.gz'
    checksum = hashlib.sha256(fetch(url, timeout=120)).hexdigest()
    specs['zapret2-source'] = {'version': version, 'architecture': 'all', 'url': url, 'sha256': checksum}
    return specs


def resolve_image(repository, pushed):
    raw = json.loads(command(['docker', 'buildx', 'imagetools', 'inspect', '--raw', repository + '@' + digest(pushed)]))
    if 'manifests' in raw:
        manifests = [(m['platform']['architecture'], m['digest']) for m in raw['manifests']
                     if m.get('platform', {}).get('os') == 'linux']
    else:
        manifests = [('amd64', pushed)]
    result = {}
    for arch, ref in manifests:
        details = json.loads(command(['docker', 'buildx', 'imagetools', 'inspect', '--raw', repository + '@' + ref]))
        result[arch] = {'repository': repository, 'digest': digest(ref), 'config': digest(details['config']['digest'])}
    return result


def rollback_plan(root, journal):
    """Rollback to the previous tagged release needs its exact migration history."""
    migrations = 'app/server/database/migrations/'
    git = ['git', '-C', str(root)]
    try:
        tag = command(git + ['describe', '--tags', '--abbrev=0', '--match', 'v[0-9]*', 'HEAD^']).strip()
        entries = json.loads(command(git + ['show', f'{tag}:{migrations}meta/_journal.json']))['entries']
        previous = [{'when': e['when'], 'sha256': hashlib.sha256(subprocess.run(
            git + ['show', f"{tag}:{migrations}{e['tag']}.sql"], capture_output=True, check=True, timeout=60).stdout
        ).hexdigest()} for e in entries]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, ReleaseError):
        return 'forward-only'
    # Old images refuse a database with migrations they do not know.
    return 'backward-compatible' if previous == journal else 'forward-only'


def build(argv):
    parser = argparse.ArgumentParser(prog='vpn2 release build-manifest', description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--digests', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repository', default='ghcr.io/anysda/vpn2')
    args = parser.parse_args(argv)
    root = args.root
    images = {name: resolve_image(args.repository + '/' + name, (args.digests / (name + '.txt')).read_text().strip())
              for name in ('panel', 'tgbot', 'emergency')}
    journal = json.loads((root / 'app/server/database/migrations/meta/_journal.json').read_text())
    entries = journal['entries']
    history = [{'when': e['when'], 'sha256': sha256(root / f"app/server/database/migrations/{e['tag']}.sql")}
               for e in entries]
    manifest = {
        'format': 1, 'version': json.loads((root / 'app/package.json').read_text())['version'],
        'commit': command(['git', '-C', str(root), 'rev-parse', 'HEAD']).strip(),
        'images': images, 'binaries': binary_specs(root),
        'local_service_api': {'min': 1, 'max': 1},
        'database': {'min': len(entries), 'max': len(entries), 'migrate_from': [0, len(entries)],
                     'journal': history},
        'rollback': rollback_plan(root, history), 'stages': source_files(root),
        'os': {'debian': ['12', '13'], 'ubuntu': ['24.04', '26.04']},
        'architectures': ['amd64'], 'min_free_bytes': 2147483648, 'min_memory_bytes': 268435456,
        'system_packages': {'policy': 'supported distribution repositories; security updates allowed',
                            'record': 'dpkg-query -W at successful deploy'},
    }
    args.output.write_text(json.dumps(manifest, indent=2) + '\n')



def main(argv=None):
    try:
        build(sys.argv[1:] if argv is None else argv)
    except ReleaseError as error:
        raise SystemExit(f'release manifest generation failed: {error}') from None
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        raise SystemExit('release manifest generation failed: ' + type(error).__name__) from None
    return 0
