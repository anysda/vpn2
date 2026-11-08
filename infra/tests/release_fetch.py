"""Download a GitHub release asset for CI, with the releases API as a fallback.

github.com/<repo>/releases/download sometimes answers 5xx while the API still
serves the same file, so a failed download goes through
api.github.com/repos/<repo>/releases/tags/<tag> -> asset by name -> asset url
with Accept: application/octet-stream. GITHUB_TOKEN, when set, goes to the API
only, never to the storage host the asset redirects to. The checksum is checked
by the caller after either path. A silent download page costs one WEB_TIMEOUT
(a stall between reads, not the whole download) and goes to the API unretried.

CLI: release_fetch.py <owner/repo> <tag> <asset> <out> --sha256 <hex>
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 120
WEB_TIMEOUT = 10


class _SameHostAuth(urllib.request.HTTPRedirectHandler):
    """Drop Authorization when a redirect leaves the host: signed storage URLs reject a second auth."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.headers.pop('Authorization', None)
            new.unredirected_hdrs.pop('Authorization', None)
        return new


_OPENER = urllib.request.build_opener(_SameHostAuth)


def _get(url, headers=None, timeout=TIMEOUT):
    request = urllib.request.Request(url, headers=headers or {})
    with _OPENER.open(request, timeout=timeout) as response:
        return response.read()


def _timed_out(error):
    return isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError)


def _retry(what, call, tries=3, final=lambda _error: False):
    for attempt in range(1, tries + 1):
        try:
            return call()
        except (urllib.error.URLError, OSError) as error:
            print(f'{what}: {error} (try {attempt}/{tries})', file=sys.stderr)
            if attempt == tries or final(error):
                raise
            time.sleep(2 * attempt)


def fetch(repo, tag, name):
    """Bytes of release asset <name> of <repo> at <tag>: download page first, then the API."""
    web = os.environ.get('RELEASE_FETCH_WEB', 'https://github.com')
    api = os.environ.get('RELEASE_FETCH_API', 'https://api.github.com')
    try:
        return _retry('releases/download', lambda: _get(f'{web}/{repo}/releases/download/{tag}/{name}',
                                                        timeout=WEB_TIMEOUT), final=_timed_out)
    except (urllib.error.URLError, OSError) as error:
        print(f'releases/download failed ({error}), trying the releases API', file=sys.stderr)
    headers = {'Accept': 'application/vnd.github+json'}
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    release = json.loads(_retry('releases API', lambda: _get(f'{api}/repos/{repo}/releases/tags/{tag}', headers)))
    url = next((a['url'] for a in release.get('assets', []) if a.get('name') == name), None)
    if url is None:
        raise LookupError(f'{repo} {tag}: no asset {name}')
    headers['Accept'] = 'application/octet-stream'
    return _retry('release asset', lambda: _get(url, headers))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('repo')
    parser.add_argument('tag')
    parser.add_argument('name')
    parser.add_argument('out', type=Path)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    try:
        data = fetch(args.repo, args.tag, args.name)
    except (urllib.error.URLError, OSError, LookupError, ValueError) as error:
        sys.exit(f'{args.name}: {error}')
    got = hashlib.sha256(data).hexdigest()
    if got != args.sha256.lower():
        sys.exit(f'{args.name}: sha256 mismatch: got {got}, want {args.sha256}')
    args.out.write_bytes(data)


if __name__ == '__main__':
    main()
