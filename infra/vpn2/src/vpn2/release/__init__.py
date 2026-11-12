"""Release contract, immutable images and verified download cache (stdlib only).

Runs on a node before the vpn2 venv exists: the package directory is shipped
next to the stages and started as `python3 -m vpn2.release`.
"""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from vpn2.fsutil import read_json

# CI publishes each images build's manifest next to the panel image, tagged
# release-<full commit>: the package is public, anonymous pull needs no account.
RELEASE_SOURCE = 'https://ghcr.io/anysda/vpn2/panel'
MANIFEST_MEDIA_TYPE = 'application/vnd.anysda.vpn2.release-manifest.v1+json'
# A freshly started dockerd can break a layer extraction (snapshot not found,
# context canceled); the same pull a few seconds later succeeds.
PULL_PAUSES = (5, 15)
COMMAND_TIMEOUT = 300
# An image is hundreds of megabytes: at 250 KB/s it does not fit the command limit.
# Docker keeps the layers it has finished, so the next attempt continues the download.
PULL_TIMEOUT = 1200
# releases/download can answer 503/504 for an hour while the releases API works:
# transient errors are retried, then a github.com asset is taken through the API.
DOWNLOAD_PAUSES = (3, 10, 30)
GITHUB_DOWNLOAD = 'https://github.com'
GITHUB_API = 'https://api.github.com'


class ReleaseError(Exception):
    detail = ''


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


def command(args, timeout=None):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout or COMMAND_TIMEOUT)
    if result.returncode:
        # Registry output and docker inspect may contain credentials. Never echo it.
        error = ReleaseError(f'{args[0]} {args[1]} failed')
        if args[:2] == ['docker', 'pull']:
            error.detail = pull_error(result.stderr)
        raise error
    return result.stdout


def pull_error(stderr):
    """Last line of a docker pull error, credentials in URLs and tokens masked."""
    lines = [line.strip() for line in (stderr or '').splitlines() if line.strip()]
    line = lines[-1] if lines else 'no error text'
    line = re.sub(r'(?i)(://[^/\s@]*:|bearer\s+|token=)[^\s@&]+', r'\1***', line)
    return line[:200]


def pull_image(component, selected):
    """docker pull with retries; returns the last docker error, '' on success."""
    error = ''
    attempts = len(PULL_PAUSES) + 1
    for attempt in range(1, attempts + 1):
        try:
            command(['docker', 'pull', selected], PULL_TIMEOUT)
            return ''
        except ReleaseError as exc:
            error = exc.detail or str(exc)
        except subprocess.TimeoutExpired:
            error = (f'no answer in {PULL_TIMEOUT} s (the link to the image registry is too slow '
                     'or stalled; finished layers are kept)')
        if attempt < attempts:
            pause = PULL_PAUSES[attempt - 1]
            print(f'release: {component}: docker pull failed ({attempt}/{attempts}), '
                  f'retry in {pause} s: {error}', file=sys.stderr)
            time.sleep(pause)
    return error


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
    failure = ''
    if pull:
        # A failed pull is not final: a local image must still pass the immutable digest check.
        failure = pull_image(component, selected)
    try:
        try:
            actual = json.loads(command(['docker', 'image', 'inspect', selected]))[0]
        except ReleaseError:
            if requested:
                raise
            # docker save/load need not preserve RepoDigests. The config digest binds
            # the ordered rootfs DiffIDs as well as the executable configuration.
            actual = json.loads(command(['docker', 'image', 'inspect', expected['config']]))[0]
    except ReleaseError as exc:
        if not failure:
            raise ReleaseError(f'{component}: image {selected} is not on this node '
                               f'({exc})') from exc
        raise ReleaseError(f'{component}: image {selected} not downloaded after '
                           f'{len(PULL_PAUSES) + 1} attempts, docker: {failure}. Check that the '
                           'entry reaches the image registry and run ./deploy.sh again') from exc
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
    # Legacy save/load can lose RepoDigests. Only the classic representation
    # (no content descriptor) can use Id as the config digest: it binds the
    # executable configuration and ordered rootfs DiffIDs.
    descriptor = actual.get('Descriptor') or {}
    matches = (reference in (actual.get('RepoDigests') or []) or descriptor.get('digest') == expected['digest']
               or not descriptor and actual['Id'] == expected['config'])
    if not matches or actual['Architecture'] != arch:
        raise ReleaseError(f'{component}: digest mismatch; image rejected')
    return digest(actual['Id'])  # Store-native immutable handle, never a tag.


def download(name, url, output, headers=None, retried=(429,), timeout=60):
    """GET url into output with retries; returns (error text, attempts), ('', n) on success."""
    attempts = len(DOWNLOAD_PAUSES) + 1
    for attempt in range(1, attempts + 1):
        try:
            output.seek(0)
            output.truncate()
            request = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(request, timeout=timeout) as source:
                shutil.copyfileobj(source, output)
            return '', attempt
        except urllib.error.HTTPError as exc:
            error = f'HTTP {exc.code}'
            if exc.code < 500 and exc.code not in retried:
                return error, attempt
        except (OSError, http.client.HTTPException) as exc:
            error = str(getattr(exc, 'reason', '') or exc) or type(exc).__name__
        if attempt < attempts:
            pause = DOWNLOAD_PAUSES[attempt - 1]
            print(f'release: {name}: {url}: {error} ({attempt}/{attempts}), retry in {pause} s', file=sys.stderr)
            time.sleep(pause)
    return error, attempts


def github_download(name, url, output):
    """A github.com release asset through the releases API; returns an error text, '' on success."""
    parts = url.removeprefix(GITHUB_DOWNLOAD + '/').split('/')
    if not url.startswith(GITHUB_DOWNLOAD + '/') or len(parts) != 6 or parts[2:4] != ['releases', 'download']:
        return 'not a GitHub release asset, no fallback'
    owner, repo, _releases, _download, tag, asset = parts
    api = f'{GITHUB_API}/repos/{owner}/{repo}/releases/tags/{tag}'
    print(f'release: {name}: trying GitHub releases API {api}', file=sys.stderr)
    with tempfile.TemporaryFile() as listing:
        error, attempts = download(name, api, listing)
        if error:
            return f'{api}: {error} after {attempts} attempts'
        listing.seek(0)
        try:
            found = [item['url'] for item in json.load(listing)['assets']
                     if item['name'] == urllib.parse.unquote(asset)]
        except (ValueError, KeyError, TypeError):
            return f'{api}: unexpected answer'
    if not found:
        return f'{api}: no asset {asset}'
    error, attempts = download(name, found[0], output, {'Accept': 'application/octet-stream'})
    return f'{found[0]}: {error} after {attempts} attempts' if error else ''


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
            with os.fdopen(fd, 'wb') as output:
                error, attempts = download(name, item['url'], output)
                if error:
                    fallback = github_download(name, item['url'], output)
                    if fallback:
                        raise ReleaseError(f'{name}: {item["url"]}: {error} after {attempts} attempts; '
                                           f'{fallback}. Check that the entry reaches github.com '
                                           'and run ./deploy.sh again')
            if sha256(temporary) != item['sha256']:
                raise ReleaseError(f'{name}: digest mismatch for {target} after a fresh download; '
                                   'artifact rejected before extraction')
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if destination:
        shutil.copyfile(target, destination)
    return target


def versions(path):
    """Flat `key: "value"` pins of infra/versions.yml; PyYAML reads the same."""
    pins = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([a-z0-9_]+):\s*"([^"]+)"\s*', line)
        if match:
            pins[match[1]] = match[2]
        elif line.strip() and not line.lstrip().startswith('#'):
            raise ReleaseError(f'{path}: not a pin line: {line.strip()[:60]}')
    return pins


def source_files(root):
    paths = list((root / 'infra/scripts').glob('*.sh'))
    paths += list((root / 'infra/lib').glob('*.sh')) + list((root / 'infra/lib').glob('*.py'))
    paths += list((root / 'infra/configs').rglob('*'))
    # The package and the roles are deploy code as much as the stages are.
    paths += list((root / 'infra/vpn2/src/vpn2').rglob('*.py'))
    paths += [path for path in (root / 'infra/ansible').rglob('*')
              if not any(part.startswith('.') or part == '__pycache__'
                         for part in path.relative_to(root).parts)]
    paths += [root / 'infra/vpn2/pyproject.toml', root / 'infra/versions.yml']
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


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ['build-manifest']:
        from . import build
        return build.main(argv[1:])
    parser = argparse.ArgumentParser(prog='vpn2 release', description=__doc__)
    # Without a manifest only artifact works: development mode, the hash pinned in the stage.
    parser.add_argument('--manifest')
    sub = parser.add_subparsers(dest='action', required=True)
    check = sub.add_parser('verify-tree')
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
    art.add_argument('--sha256')
    args = parser.parse_args(argv)
    try:
        if args.manifest is None:
            if args.action != 'artifact' or not args.url or not re.fullmatch(r'[a-f0-9]{64}', args.sha256 or ''):
                raise ReleaseError(f'{getattr(args, "name", args.action)}: without --manifest only artifact '
                                   'with --url and the pinned --sha256 is allowed')
            manifest = {'binaries': {args.name: {'url': args.url, 'sha256': args.sha256}}}
        elif args.action == 'fetch':
            fetch(args.root, args.manifest, args.source)
            return 0
        else:
            manifest = load(args.manifest)
        if args.action == 'verify-tree':
            validate_source(manifest, args.root)
            print(f'release: манифест commit {manifest["commit"]} (версия {manifest["version"]}) '
                  'проверен, стадии и миграции совпадают')
        elif args.action == 'image':
            print(image(manifest, args.component, args.requested, not args.offline))
        else:
            if args.url and args.url != manifest['binaries'][args.name]['url']:
                raise ReleaseError('artifact version/URL differs from release manifest')
            if args.sha256 and args.sha256 != manifest['binaries'][args.name]['sha256']:
                raise ReleaseError(f'{args.name}: hash pinned in the stage differs from release manifest')
            artifact(manifest, args.name, args.cache, args.destination)
    except (ReleaseError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, ReleaseError) else type(error).__name__
        if args.action in ('fetch', 'verify-tree'):
            print('✗ ' + message, file=sys.stderr)
        else:
            print('release: ' + message + '; no unverified artifact accepted', file=sys.stderr)
        return 1
    return 0

