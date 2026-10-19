#!/usr/bin/env python3
"""Release contract, immutable images and verified download cache (stdlib only)."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request


class ReleaseError(Exception):
    pass


def read_json(path):
    return json.loads(Path(path).read_text())


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', value):
        raise ReleaseError('invalid release digest')
    return value


def architecture():
    return {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine(), platform.machine())


def load(path):
    try:
        manifest = read_json(path)
    except FileNotFoundError as exc:
        raise ReleaseError(
            'release manifest not found; download release-<commit> from a successful images workflow '
            '(GitHub login required), checkout that commit and run '
            './deploy.sh --manifest <path>/release-manifest.json; '
            'see README.md Quickstart step 2. For development only: ./deploy.sh --development'
        ) from exc
    if manifest.get('format') != 1 or not re.fullmatch(r'[a-f0-9]{40}', manifest['commit']):
        raise ReleaseError('unsupported release manifest')
    if manifest['rollback'] not in ('backward-compatible', 'forward-only'):
        raise ReleaseError('unsupported rollback plan')
    for images in manifest['images'].values():
        for item in images.values():
            digest(item['digest'])
            digest(item['config'])
            if not re.fullmatch(r'[a-z0-9./_-]+', item['repository']):
                raise ReleaseError('invalid image repository')
    for item in manifest['binaries'].values():
        digest('sha256:' + item['sha256'])
        if not item['url'].startswith('https://'):
            raise ReleaseError('release download requires HTTPS')
    return manifest


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if result.returncode:
        # Registry output and docker inspect may contain credentials. Never echo it.
        raise ReleaseError(f'{args[0]} {args[1]} failed')
    return result.stdout


def image(manifest, component, requested='', pull=True, arch=None):
    arch = arch or architecture()
    if requested and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/@:-]*', requested):
        raise ReleaseError('invalid image reference')
    if requested.rsplit(':', 1)[-1] in ('dev', 'main', 'latest'):
        raise ReleaseError('floating tag requires explicit development mode')
    try:
        expected = manifest['images'][component][arch]
    except KeyError as exc:
        raise ReleaseError('image architecture is not supported') from exc
    reference = expected['repository'] + '@' + expected['digest']
    selected = requested or reference
    if pull:
        try:
            command(['docker', 'pull', selected])
        except (ReleaseError, subprocess.TimeoutExpired):
            pass  # A local image must still pass the immutable digest check.
    try:
        actual = json.loads(command(['docker', 'image', 'inspect', selected]))[0]
    except ReleaseError:
        if requested:
            raise
        # docker save/load need not preserve RepoDigests. The config digest binds
        # the ordered rootfs DiffIDs as well as the executable configuration.
        actual = json.loads(command(['docker', 'image', 'inspect', expected['config']]))[0]
    descriptor = actual.get('Descriptor') or {}
    if descriptor.get('mediaType') in ('application/vnd.oci.image.index.v1+json',
                                      'application/vnd.docker.distribution.manifest.list.v2+json'):
        # CI tags can point at an index (also for a single architecture plus
        # attestations). Resolve the selected immutable index locally, never
        # look up the mutable tag again or substitute an unrelated cached image.
        actual = json.loads(command(['docker', 'image', 'inspect', '--platform',
                                     'linux/' + arch, digest(actual['Id'])]))[0]
    # Classic Docker uses the config digest as Id; the containerd store uses
    # a manifest/index digest. RepoDigests bind the selected image to the
    # release on both stores, including when selected is a mutable tag.
    descriptor = actual.get('Descriptor') or {}
    matches = (reference in (actual.get('RepoDigests') or [])
               or descriptor.get('digest') == expected['digest'])
    # Legacy save/load can lose RepoDigests. Only the classic representation
    # (no content descriptor) can use Id as the config digest: it binds the
    # executable configuration and ordered rootfs DiffIDs.
    if not descriptor:
        matches = matches or actual['Id'] == expected['config']
    if not matches or actual['Architecture'] != arch:
        raise ReleaseError(f'{component}: digest mismatch; image rejected')
    return digest(actual['Id'])  # Store-native immutable handle, never a tag.


def artifact(manifest, name, cache, destination=None):
    item = manifest['binaries'][name]
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache / item['sha256']
    if target.exists() and sha256(target) != item['sha256']:
        raise ReleaseError(f'{name}: digest mismatch; artifact rejected before extraction')
    if not target.exists():
        fd, temporary = tempfile.mkstemp(dir=cache)
        try:
            with os.fdopen(fd, 'wb') as output, urllib.request.urlopen(item['url'], timeout=60) as source:
                shutil.copyfileobj(source, output)
            if sha256(temporary) != item['sha256']:
                raise ReleaseError(f'{name}: digest mismatch; artifact rejected before extraction')
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if destination:
        shutil.copyfile(target, destination)
    return target


def source_files(root):
    paths = list((root / 'infra/scripts').glob('*.sh'))
    paths += list((root / 'infra/lib').glob('*.sh')) + list((root / 'infra/lib').glob('*.py'))
    paths += list((root / 'infra/configs').rglob('*'))
    paths += list((root / 'app/server/database/migrations').rglob('*'))
    paths += [root / 'infra/deploy.sh', root / 'deploy.sh']
    return {str(path.relative_to(root)): sha256(path) for path in sorted(paths) if path.is_file()}


def validate_source(manifest, root):
    if source_files(Path(root)) != manifest['stages']:
        raise ReleaseError('release stages/migrations do not match manifest; use matching source checkout')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    check = sub.add_parser('check')
    check.add_argument('root')
    img = sub.add_parser('image')
    img.add_argument('component', choices=('panel', 'tgbot', 'emergency'))
    img.add_argument('--requested', default='')
    img.add_argument('--offline', action='store_true')
    fetch = sub.add_parser('artifact')
    fetch.add_argument('name')
    fetch.add_argument('destination')
    fetch.add_argument('--cache', default='/var/cache/vpn2-release')
    fetch.add_argument('--url')
    args = parser.parse_args()
    try:
        manifest = load(args.manifest)
        if args.action == 'check':
            validate_source(manifest, args.root)
            print('release source: verified')
        elif args.action == 'image':
            print(image(manifest, args.component, args.requested, not args.offline))
        else:
            if args.url and args.url != manifest['binaries'][args.name]['url']:
                raise ReleaseError('artifact version/URL differs from release manifest')
            artifact(manifest, args.name, args.cache, args.destination)
    except (ReleaseError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, ReleaseError) else type(error).__name__
        print('release: ' + message + '; no unverified artifact accepted', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
