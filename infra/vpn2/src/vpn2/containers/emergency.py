"""Статика страницы аварийки из образа: vpn2 containers emergency-static <каталог аварийки>.

Реестр нужен только здесь; остановленный контейнер без сети держит образ занятым и
показывает версию в `docker ps -a`. Печатает image=<образ> и changed=1, если статика сменилась.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from vpn2.containers import compose
from vpn2.release import ReleaseError

CONTAINER = 'vpn2-emergency'
REQUIRED = ('index.html', 'instructions.html')


def docker(*args, check=True):
    return subprocess.run(['docker', *args], stdout=subprocess.PIPE, text=True, check=check).stdout.strip()


def readable(root):
    """chmod -R u=rwX,go=rX: каталоги и исполняемые 755, остальное 644."""
    for path in (root, *root.rglob('*')):
        executable = path.is_dir() or path.stat().st_mode & 0o111
        path.chmod(0o755 if executable else 0o644)


def install(home, image):
    """Статика и release.json образа в каталог аварийки; False - на узле уже он."""
    held = subprocess.run(['docker', 'inspect', '--format', '{{.Image}}', CONTAINER],
                          capture_output=True, text=True).stdout.strip()
    try:
        recorded = json.loads((home / 'release.json').read_text())['image']
    except (OSError, ValueError, KeyError):
        recorded = ''
    if held == docker('image', 'inspect', '--format', '{{.Id}}', image) and recorded == image \
            and (home / 'static/index.html').is_file():
        return False
    subprocess.run(['docker', 'rm', '-f', CONTAINER], capture_output=True)
    docker('create', '--name', CONTAINER, '--network', 'none', image)
    with tempfile.TemporaryDirectory(prefix='.staging.', dir=home) as staging:
        staging = Path(staging)
        docker('cp', f'{CONTAINER}:/opt/vpn2-emergency/.', f'{staging}/')
        static = staging / 'static'
        for required in REQUIRED:
            if not (static / required).is_file():
                sys.exit(f'ERROR: в образе аварийки {image} нет статики страницы ({required})')
        # Статика меняется целиком: файл, которого в новой версии нет, на узле не остаётся.
        readable(static)
        shutil.rmtree(home / 'static.old', ignore_errors=True)
        if (home / 'static').is_dir():
            (home / 'static').rename(home / 'static.old')
        static.rename(home / 'static')
        shutil.rmtree(home / 'static.old', ignore_errors=True)
        release = json.loads((staging / 'image.json').read_text())
        record = staging / 'release.json'
        record.write_text(json.dumps({'version': release['version'], 'revision': release['revision'],
                                      'image': image}) + '\n')
        record.chmod(0o644)
        os.replace(record, home / 'release.json')
    # На узле остаётся ровно установленный образ; прежний без тега виден только с --all.
    for old in sorted(set(docker('images', '--all', '--quiet', '--no-trunc',
                                 '--filter', 'label=vpn2.component=emergency').split())):
        if old != image:
            docker('image', 'rm', '-f', old)
    return True


def main(argv):
    if len(argv) != 1:
        sys.stderr.write('Использование: vpn2 containers emergency-static <каталог аварийки>\n')
        return 2
    try:
        image = compose.release_image(os.environ, 'emergency', compose.Docker())
    except (compose.EngineError, ReleaseError, OSError, ValueError, KeyError) as error:
        sys.exit(f'release: emergency: {error}; no unverified artifact accepted')
    print(f'image={image}', flush=True)
    if install(Path(argv[0]), image):
        print('changed=1')
    return 0
