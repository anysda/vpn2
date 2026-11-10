"""Каталог набора бэкапа: sha компонентов и manifest.json (байт в байт со старым скриптом)."""
import hashlib
import json
import os
from pathlib import Path

from vpn2 import fsutil

MANIFEST = 'manifest.json'


def sha_file(path):
    """sha256 содержимого файла в виде 'sha256:<hex>'."""
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1 << 20), b''):
            digest.update(chunk)
    return 'sha256:' + digest.hexdigest()


def sha_dir(path):
    """sha256 каталога: по отсортированному обходу - относительный путь, 0, sha файла, перевод строки.

    Пустые каталоги и ссылки на каталоги в хэш не входят; нечитаемый файл - '<unreadable>'.
    """
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        files.sort()
        for name in files:
            full = os.path.join(root, name)
            digest.update(os.path.relpath(full, path).encode())
            digest.update(b'\0')
            try:
                digest.update(sha_file(full).encode())
            except Exception:  # как в старом коде: любой отказ чтения
                digest.update(b'<unreadable>')
            digest.update(b'\n')
    return 'sha256:' + digest.hexdigest()


def _sha(path):
    return sha_dir(path) if os.path.isdir(path) else sha_file(path)


def components(work_dir):
    """Имя -> sha для всего в каталоге набора, кроме manifest.json, по порядку имён."""
    return {name: _sha(os.path.join(work_dir, name))
            for name in sorted(os.listdir(work_dir)) if name != MANIFEST}


def manifest(work_dir, *, version, git_commit, schema_version, timestamp):
    """Словарь манифеста: поля и порядок как в старом скрипте."""
    return {
        'version': version,
        'git_commit': git_commit,
        'schema_version': int(schema_version),
        'timestamp': timestamp,
        'components': components(work_dir),
        'archive_format': 'tar.gz.age',
        'spec_version': 1,
    }


def write_manifest(work_dir, data):
    """manifest.json: отступ 2, ensure_ascii=False, перевод строки в конце, права 0644 (umask 022 у bash)."""
    target = Path(work_dir) / MANIFEST
    fsutil.atomic(target, json.dumps(data, indent=2, ensure_ascii=False) + '\n', mode=0o644)
    return target


def verify_components(work_dir, manifest):
    """Имена компонентов манифеста, которых нет или чей sha не сошёлся (как сверка в restore)."""
    bad = []
    for name, expected in manifest.get('components', {}).items():
        full = os.path.join(work_dir, name)
        if not os.path.exists(full) or _sha(full) != expected:
            bad.append(name)
    return bad
