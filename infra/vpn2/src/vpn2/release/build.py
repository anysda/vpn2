"""Build the published contract from this checkout and pushed image digests.

Every pinned version and hash comes from infra/versions.yml, nowhere else.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

from . import GITHUB_API, ReleaseError, command, digest, download, sha256, source_files, versions


def fetch(url, timeout=60):
    """GET url with retries; the workflow token lifts the anonymous API limit of a shared runner."""
    token = os.environ.get('GITHUB_TOKEN')
    headers = {'Authorization': f'Bearer {token}'} if token and url.startswith(GITHUB_API + '/') else {}
    body = io.BytesIO()
    # 403 is the GitHub API rate limit; other 4xx will not change on retry.
    error, _attempts = download('build-manifest', url, body, headers, retried=(403, 429), timeout=timeout)
    if error:
        raise ReleaseError(f'{url}: {error}')
    return body.getvalue()


def spec(version, arch, url, checksum=None):
    """A manifest entry; without a pinned hash the file is downloaded and hashed now."""
    return {'version': version, 'architecture': arch, 'url': url,
            'sha256': checksum or hashlib.sha256(fetch(url, timeout=120)).hexdigest()}


# Assets whose hash GitHub publishes: name, pin, repository, tag and asset templates.
GITHUB_ASSETS = (
    ('wgcf', 'wgcf', 'ViRb3/wgcf', 'v{v}', 'wgcf_{v}_linux_amd64'),
    ('caddy', 'caddy', 'caddyserver/caddy', 'v{v}', 'caddy_{v}_linux_amd64.deb'),
    ('node-exporter', 'node_exporter', 'prometheus/node_exporter', 'v{v}', 'node_exporter-{v}.linux-amd64.tar.gz'),
    ('adguard', 'adguard', 'AdguardTeam/AdGuardHome', '{v}', 'AdGuardHome_linux_amd64.tar.gz'),
)


def binary_specs(root):
    pins = versions(root / 'infra/versions.yml')
    version = pins['sing_box']
    specs = {'sing-box-' + arch: spec(
        version, arch, f'https://github.com/SagerNet/sing-box/releases/download/v{version}/sing-box-{version}-linux-{arch}.tar.gz',
        pins['sing_box_sha256_' + arch]) for arch in ('amd64', 'arm64')}
    for name, pin, repo, tag, asset in GITHUB_ASSETS:
        version = pins[pin]
        release = json.loads(fetch(f'https://api.github.com/repos/{repo}/releases/tags/{tag.format(v=version)}'))
        found = next(item for item in release['assets'] if item['name'] == asset.format(v=version))
        specs[name] = spec(version, 'amd64', found['browser_download_url'], digest(found['digest']).split(':')[1])
    version = pins['yt_node']
    specs['yt-node'] = spec(version, 'amd64', f'https://nodejs.org/dist/{version}/node-{version}-linux-x64.tar.xz',
                            pins['yt_node_sha256'])
    version = pins['yt_pot']
    specs['yt-pot'] = spec(version, 'all', f'https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/{version}.tar.gz',
                           pins['yt_pot_sha256'])
    version = pins['aws_cli']
    url = f'https://awscli.amazonaws.com/awscli-exe-linux-x86_64-{version}.zip'
    specs['aws-cli'] = spec(version, 'amd64', url)
    specs['aws-cli-signature'] = spec(version, 'amd64', url + '.sig')
    version = pins['zapret2']
    specs['zapret2-source'] = spec(version, 'all', f'https://github.com/bol-van/zapret2/archive/{version}.tar.gz')
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
