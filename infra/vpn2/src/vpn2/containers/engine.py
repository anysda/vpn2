"""Docker и compose на entry: образ выпуска, файл compose.yaml, запуск службы."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from vpn2 import fsutil, release
from vpn2.adguard.stage import resource_tool
from vpn2.containers import compose

COMPOSE_FILE = '/etc/vpn2/compose/compose.yaml'
MANIFEST = '/tmp/anysda/release-manifest.json'
STAMPS = '/var/anysda/.stamps'
PROJECT_LABEL = 'com.docker.compose.project'


class EngineError(Exception):
    pass


def say(text):
    print(f'[{os.environ.get("HOST_TAG") or "anysda"}] {text}', flush=True)


def _run(argv, **kwargs):
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('text', True)
    return subprocess.run(argv, check=False, **kwargs)


class Docker:
    """Вызовы docker одной точкой: тесты подменяют run."""

    def __init__(self, run=_run, compose_file=COMPOSE_FILE):
        self.run = run
        self.compose_file = str(compose_file)

    def call(self, *args, check=True, **kwargs):
        result = self.run(['docker', *args], **kwargs)
        if check and result.returncode:
            detail = (result.stderr or '').strip().splitlines()[-1:] or ['']
            raise EngineError(f'docker {" ".join(args[:2])}: код {result.returncode} {detail[0][:300]}')
        return result

    def inspect(self, name):
        """docker inspect контейнера или None, когда его нет."""
        result = self.call('inspect', '--type', 'container', name, check=False)
        if result.returncode:
            return None
        return json.loads(result.stdout)[0]

    def image_id(self, reference):
        result = self.call('image', 'inspect', '--format', '{{.Id}}', reference, check=False)
        return result.stdout.strip() if result.returncode == 0 else ''

    def logs(self, name, tail=None):
        args = ['logs'] + (['--tail', str(tail)] if tail else []) + [name]
        result = self.call(*args, check=False)
        return (result.stdout or '') + (result.stderr or '')

    def services(self):
        """Службы из compose.yaml в том виде, в каком их принимает publish."""
        try:
            found = compose.load(Path(self.compose_file).read_text())
        except OSError:
            return {}
        return {name: compose.unescape(service) for name, service in found.items()}

    def publish(self, name, service):
        """Записать службу в compose.yaml; вернуть, сменился ли файл."""
        path = Path(self.compose_file)
        try:
            text = path.read_text()
        except OSError:
            text = ''
        fresh, changed = compose.with_service(text, name, service)
        if changed:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fsutil.atomic(path, fresh, mode=0o600, encoding='utf-8')
        return changed

    def up(self, name):
        """Привести контейнер к описанию службы; вернуть, был ли он создан заново.

        Контейнер с тем же именем, запущенный не compose (docker run прошлых выпусков),
        снимается: данные лежат в каталогах хоста и переживают замену.
        """
        before = self.inspect(name)
        if before and (before['Config'].get('Labels') or {}).get(PROJECT_LABEL) != compose.PROJECT:
            self.call('rm', '-f', name)
        self.call('compose', '-f', self.compose_file, 'up', '-d', '--no-deps', name)
        after = self.inspect(name)
        if after is None:
            raise EngineError(f'{name}: контейнера нет после compose up')
        return before is None or before['Id'] != after['Id']


def release_image(environ, component, requested='', pull=True, *, docker=None, err=sys.stderr):
    """Идентификатор образа компонента: выпуск по digest манифеста, в разработке - тег :dev."""
    docker = docker or Docker()
    if environ.get('VPN2_DEVELOPMENT', 'false') != 'true':
        manifest = release.load(environ.get('VPN2_RELEASE_MANIFEST') or MANIFEST)
        return release.image(manifest, component, requested, pull)
    reference = requested or f'ghcr.io/anysda/vpn2/{component}:dev'
    source = 'pulled'
    if not pull:
        source = 'local, pull disabled'
    elif docker.call('pull', reference, check=False).returncode:
        source = 'local, pull failed'
        print(f'WARNING: [development] {component}: pull {reference} failed, using the local image', file=err)
    image = docker.image_id(reference)
    if not image:
        raise EngineError(f'{component}: образа {reference} нет')
    revision = docker.call('image', 'inspect', '--format',
                           '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                           reference, check=False).stdout.strip()
    print(f'[development] {component}: {reference} revision {revision or "unknown"} ({source})', file=err)
    return image


def resource_text(environ, service):
    return resource_tool(environ, '--service', service)


def http_ok(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait(probe, attempts, pause, sleep=time.sleep):
    for attempt in range(attempts):
        if probe():
            return True
        if attempt + 1 < attempts:
            sleep(pause)
    return False


def drop_timezone_dir(path='/etc/timezone'):
    """В Ubuntu 26.04 файла нет: docker создавал на его месте каталог, контейнер не стартовал."""
    try:
        if os.path.isdir(path):
            os.rmdir(path)
    except OSError:
        pass


def stamp(stage, directory=STAMPS):
    os.makedirs(directory, exist_ok=True)
    Path(directory, stage).touch()
