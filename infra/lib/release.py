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
import urllib.error
import urllib.parse
import urllib.request

# CI publishes each images build's manifest next to the panel image, tagged
# release-<full commit>: the package is public, anonymous pull needs no account.
RELEASE_SOURCE = 'https://ghcr.io/anysda/vpn2/panel'
MANIFEST_MEDIA_TYPE = 'application/vnd.anysda.vpn2.release-manifest.v1+json'


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
        raise ReleaseError(f'release manifest not found: {path}. ./deploy.sh without flags downloads '
                           'the manifest of the checked-out commit; --manifest is only for work without network') from exc
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
        # A damaged cache entry is fetched again once, never trusted or kept.
        target.unlink(missing_ok=True)
    if not target.exists():
        fd, temporary = tempfile.mkstemp(dir=cache)
        try:
            with os.fdopen(fd, 'wb') as output, urllib.request.urlopen(item['url'], timeout=60) as source:
                shutil.copyfileobj(source, output)
            if sha256(temporary) != item['sha256']:
                raise ReleaseError(f'{name}: digest mismatch for {target} after a fresh download; '
                                   'artifact rejected before extraction')
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
    actual = source_files(Path(root))
    if actual != manifest['stages']:
        changed = sorted(path for path in set(actual) | set(manifest['stages'])
                         if actual.get(path) != manifest['stages'].get(path))
        listed = ', '.join(changed[:5]) + (f' и ещё {len(changed) - 5}' if len(changed) > 5 else '')
        raise ReleaseError(f'стадии и миграции дерева не совпадают с проверенной сборкой '
                           f'{manifest["commit"][:12]}: {listed}. Верните файлы (git status, git stash) '
                           'или для разработки: ./deploy.sh --development')


def git(root, *args):
    # The checkout is the code deploy is about to run: trusting its owner is no new risk.
    return subprocess.run(['git', '-c', f'safe.directory={root}', '-C', str(root), *args],
                          capture_output=True, text=True, timeout=30)


def registry_read(source, path, accept):
    """GET from an OCI registry; an anonymous pull token is fetched when asked for."""
    url = urllib.parse.urlsplit(source)
    repository = url.path.strip('/')
    request = urllib.request.Request(f'{url.scheme}://{url.netloc}/v2/{repository}/{path}',
                                     headers={'Accept': accept})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read(1 << 20)
    except urllib.error.HTTPError as error:
        challenge = error.headers.get('WWW-Authenticate', '')
        if error.code != 401 or not challenge.lower().startswith('bearer '):
            raise
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    query = urllib.parse.urlencode({'service': params.get('service', ''),
                                    'scope': params.get('scope', f'repository:{repository}:pull')})
    with urllib.request.urlopen(params['realm'] + '?' + query, timeout=60) as response:
        token = json.load(response)
    request.add_header('Authorization', 'Bearer ' + (token.get('token') or token['access_token']))
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(1 << 20)


def fetch(root, output, source):
    """Download the manifest of the checked-out commit; explain what to do if absent."""
    try:
        head = git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
    except OSError:
        head = None
    if head is None or head.returncode:
        raise ReleaseError('commit дерева не определить: нет git или каталог не из git clone. '
                           'Поставьте по README (git clone), без сети: ./deploy.sh --manifest <файл>')
    commit = head.stdout.strip()
    host = urllib.parse.urlsplit(source).netloc
    try:
        oci = json.loads(registry_read(source, 'manifests/release-' + commit,
                                       'application/vnd.oci.image.manifest.v1+json'))
        layer = next(item for item in oci['layers'] if item['mediaType'] == MANIFEST_MEDIA_TYPE)
        blob = registry_read(source, 'blobs/' + digest(layer['digest']), MANIFEST_MEDIA_TYPE)
    except urllib.error.HTTPError as error:
        if error.code not in (403, 404):
            raise ReleaseError(f'{host} ответил {error.code} на запрос манифеста commit {commit[:12]}; '
                               'повторите ./deploy.sh позже') from None
        published = git(root, 'for-each-ref', '--count=1', '--contains', commit, 'refs/remotes', 'refs/tags')
        if published.returncode == 0 and not published.stdout.strip():
            hint = ('этот commit есть только в вашем дереве (свои коммиты). Вернитесь к опубликованному: '
                    'git checkout <ветка или тег>')
        else:
            hint = ('сборка images для него ещё идёт или не прошла. Подождите 10-15 минут и повторите '
                    './deploy.sh, либо git checkout предыдущего проверенного commit')
        raise ReleaseError(f'манифест commit {commit[:12]} не опубликован: {hint}. '
                           'Для разработки без проверки: ./deploy.sh --development') from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, 'reason', error)
        cached = Path(output)
        try:
            if read_json(cached).get('commit') == commit:
                print(f'release: {host} недоступен ({reason}), беру манифест commit {commit[:12]}, '
                      'скачанный раньше', file=sys.stderr)
                return
        except (OSError, ValueError):
            pass
        raise ReleaseError(f'не удалось скачать манифест commit {commit[:12]} с {host}: {reason}. '
                           'Проверьте сеть и DNS и повторите ./deploy.sh; без сети: '
                           './deploy.sh --manifest <файл>') from None
    except (ValueError, KeyError, TypeError, StopIteration):
        raise ReleaseError(f'{host}: публикация release-{commit} не похожа на манифест сборки') from None
    if 'sha256:' + hashlib.sha256(blob).hexdigest() != layer['digest']:
        raise ReleaseError(f'{host}: манифест commit {commit[:12]} повреждён при загрузке (digest не совпал)')
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=Path(output).parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(blob)
        if load(temporary)['commit'] != commit:
            raise ReleaseError(f'{host}: release-{commit[:12]} содержит манифест другого commit')
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    check = sub.add_parser('check')
    check.add_argument('root')
    download = sub.add_parser('fetch')
    download.add_argument('root')
    download.add_argument('--source', default=os.environ.get('VPN2_RELEASE_SOURCE') or RELEASE_SOURCE)
    img = sub.add_parser('image')
    img.add_argument('component', choices=('panel', 'tgbot', 'emergency'))
    img.add_argument('--requested', default='')
    img.add_argument('--offline', action='store_true')
    art = sub.add_parser('artifact')
    art.add_argument('name')
    art.add_argument('destination')
    art.add_argument('--cache', default='/var/cache/vpn2-release')
    art.add_argument('--url')
    args = parser.parse_args()
    try:
        if args.action == 'fetch':
            fetch(args.root, args.manifest, args.source)
            return 0
        manifest = load(args.manifest)
        if args.action == 'check':
            validate_source(manifest, args.root)
            print(f'release: манифест commit {manifest["commit"]} (версия {manifest["version"]}) '
                  'проверен, стадии и миграции совпадают')
        elif args.action == 'image':
            print(image(manifest, args.component, args.requested, not args.offline))
        else:
            if args.url and args.url != manifest['binaries'][args.name]['url']:
                raise ReleaseError('artifact version/URL differs from release manifest')
            artifact(manifest, args.name, args.cache, args.destination)
    except (ReleaseError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, ReleaseError) else type(error).__name__
        if args.action in ('fetch', 'check'):
            print('✗ ' + message, file=sys.stderr)
        else:
            print('release: ' + message + '; no unverified artifact accepted', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
