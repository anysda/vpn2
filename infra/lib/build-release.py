#!/usr/bin/env python3
"""Build the published contract from this checkout and pushed image digests."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import urllib.request

from release import command, digest, sha256, source_files


def value(root, path, name):
    match = re.search(rf"^{name}=['\"]?([^'\"\s]+)", (root / path).read_text(), re.M)
    if match is None:
        raise ValueError('missing pinned version: ' + name)
    return match[1]


def binary_specs(root):
    stages = 'infra/scripts/'
    sb = (root / 'infra/lib/install-sing-box.sh').read_text()
    version = re.search(r"SB_VER='([^']+)'", sb)[1]
    specs = {}
    for arch in ('amd64', 'arm64'):
        specs['sing-box-' + arch] = {
            'version': version, 'architecture': arch,
            'sha256': re.search(rf"SB_SHA256_{arch.upper()}='([^']+)'", sb)[1],
            'url': f'https://github.com/SagerNet/sing-box/releases/download/v{version}/sing-box-{version}-linux-{arch}.tar.gz',
        }
    entries = (
        ('wgcf', '10-foreign.sh', 'WGCF_VER', 'ViRb3/wgcf', 'v{v}', 'wgcf_{v}_linux_amd64'),
        ('caddy', '30-frontend.sh', 'CADDY_VER', 'caddyserver/caddy', 'v{v}', 'caddy_{v}_linux_amd64.deb'),
        ('node-exporter', '00-bootstrap.sh', 'NE_VERSION', 'prometheus/node_exporter', 'v{v}', 'node_exporter-{v}.linux-amd64.tar.gz'),
        ('adguard', '22-adguard.sh', 'AGH_VER', 'AdguardTeam/AdGuardHome', '{v}', 'AdGuardHome_linux_amd64.tar.gz'),
    )
    for name, stage, variable, repo, tag, asset in entries:
        version = value(root, stages + stage, variable)
        filename = asset.format(v=version)
        url = f'https://api.github.com/repos/{repo}/releases/tags/{tag.format(v=version)}'
        with urllib.request.urlopen(url, timeout=60) as response:
            release = json.load(response)
        asset_data = next(a for a in release['assets'] if a['name'] == filename)
        specs[name] = {'version': version, 'architecture': 'amd64',
                       'url': asset_data['browser_download_url'],
                       'sha256': digest(asset_data['digest']).split(':')[1]}
    node = (root / (stages + '19-yt-zapret.sh')).read_text()
    node_url = re.search(r'https://nodejs.org/dist/[^\s]+\.tar\.xz', node)[0]
    specs['yt-node'] = {'version': node_url.split('/')[4], 'architecture': 'amd64',
                        'url': node_url, 'sha256': re.search(r"printf '%s  %s\\n' ([a-f0-9]{64})", node)[1]}
    pot = (root / 'infra/lib/install-yt-pot.sh').read_text()
    version = value(root, 'infra/lib/install-yt-pot.sh', 'pot_version')
    specs['yt-pot'] = {'version': version, 'architecture': 'all',
                       'url': f'https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/{version}.tar.gz',
                       'sha256': re.search(r"printf '%s  %s\\n' ([a-f0-9]{64})", pot)[1]}
    version = value(root, stages + '26-backup.sh', '  AWS_CLI_VERSION')
    url = f'https://awscli.amazonaws.com/awscli-exe-linux-x86_64-{version}.zip'
    with urllib.request.urlopen(url, timeout=120) as response:
        checksum = hashlib.sha256()
        while chunk := response.read(1024 * 1024):
            checksum.update(chunk)
    specs['aws-cli'] = {'version': version, 'architecture': 'amd64', 'url': url, 'sha256': checksum.hexdigest()}
    with urllib.request.urlopen(url + '.sig', timeout=60) as response:
        signature = hashlib.sha256(response.read()).hexdigest()
    specs['aws-cli-signature'] = {'version': version, 'architecture': 'amd64', 'url': url + '.sig', 'sha256': signature}
    version = re.search(r'YT_ZAPRET_REF:-([a-f0-9]{40})', node)[1]
    url = f'https://github.com/bol-van/zapret2/archive/{version}.tar.gz'
    with urllib.request.urlopen(url, timeout=120) as response:
        checksum = hashlib.sha256()
        while chunk := response.read(1024 * 1024):
            checksum.update(chunk)
    specs['zapret2-source'] = {'version': version, 'architecture': 'all', 'url': url, 'sha256': checksum.hexdigest()}
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--digests', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repository', default='ghcr.io/anysda/vpn2')
    args = parser.parse_args()
    root = args.root
    images = {name: resolve_image(args.repository + '/' + name, (args.digests / (name + '.txt')).read_text().strip())
              for name in ('panel', 'tgbot')}
    journal = json.loads((root / 'app/server/database/migrations/meta/_journal.json').read_text())
    entries = journal['entries']
    manifest = {
        'format': 1, 'version': json.loads((root / 'app/package.json').read_text())['version'],
        'commit': command(['git', '-C', str(root), 'rev-parse', 'HEAD']).strip(),
        'images': images, 'binaries': binary_specs(root),
        'local_service_api': {'min': 1, 'max': 1},
        'database': {'min': len(entries), 'max': len(entries), 'migrate_from': [0, len(entries)],
                     'journal': [{'when': e['when'], 'sha256': sha256(root / f"app/server/database/migrations/{e['tag']}.sql")}
                                 for e in entries]},
        'rollback': 'backward-compatible', 'stages': source_files(root),
        'os': {'debian': ['12', '13'], 'ubuntu': ['24.04', '26.04']},
        'architectures': ['amd64'], 'min_free_bytes': 2147483648, 'min_memory_bytes': 268435456,
        'system_packages': {'policy': 'supported distribution repositories; security updates allowed',
                            'record': 'dpkg-query -W at successful deploy'},
    }
    args.output.write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        raise SystemExit('release manifest generation failed: ' + type(error).__name__) from None
