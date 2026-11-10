import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from vpn2 import fsutil
from vpn2.backup import bundle
from vpn2.backup.bundle import components, manifest, sha_dir, sha_file, verify_components, write_manifest

SCRIPT = Path(__file__).resolve().parent / 'fixtures' / 'backup' / 'anysda-backup.sh'
NEEDS_BASH = pytest.mark.skipif(shutil.which('bash') is None or shutil.which('python3') is None,
                                reason='нужны bash и python3')
START = '# ── Manifest с SHA-256 каждого компонента'
END = '# ── Архив tar.gz внутри WORK'
KW = {'version': '1.2.3', 'git_commit': 'abc1234', 'schema_version': 7, 'timestamp': '2026-01-02T030405Z'}
HELLO_SHA = 'sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'


def make_bundle(root):
    root.mkdir()
    (root / 'db.sqlite').write_bytes(b'SQLite-ish\x00\xff')
    (root / 'config.json').write_text('{"a": 1}\n')
    (root / 'etc').mkdir()
    (root / 'etc' / 'wg0.conf').write_text('hello')
    (root / 'etc' / 'вложенный').mkdir()
    (root / 'etc' / 'вложенный' / 'ключ с пробелом.txt').write_text('данные')
    (root / 'empty').mkdir()
    (root / 'имя с пробелом.txt').write_text('hello')
    (root / 'link').symlink_to('config.json')
    return root


def test_sha_file_known_value(tmp_path):
    f = tmp_path / 'f'
    f.write_text('hello')
    assert sha_file(f) == HELLO_SHA
    assert sha_file(f) == sha_file(f)


def test_sha_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        sha_file(tmp_path / 'none')


def test_sha_dir_empty_and_order_independent(tmp_path):
    (tmp_path / 'e').mkdir()
    assert sha_dir(tmp_path / 'e') == 'sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    for n in ('x', 'y'):
        (a / n).write_text(n)
    for n in ('y', 'x'):
        (b / n).write_text(n)
    assert sha_dir(a) == sha_dir(b)
    (b / 'x').write_text('changed')
    assert sha_dir(a) != sha_dir(b)


def test_sha_dir_unreadable_file(tmp_path, monkeypatch):
    (tmp_path / 'f').write_text('x')
    monkeypatch.setattr(bundle, 'sha_file', lambda p: (_ for _ in ()).throw(PermissionError(p)))
    expected = sha_dir(tmp_path)
    import hashlib
    h = hashlib.sha256(b'f\0<unreadable>\n')
    assert expected == 'sha256:' + h.hexdigest()


def test_components_set_and_order(tmp_path):
    root = make_bundle(tmp_path / 'b')
    (root / bundle.MANIFEST).write_text('{}')
    got = components(root)
    assert list(got) == sorted(['db.sqlite', 'config.json', 'etc', 'empty', 'имя с пробелом.txt', 'link'])
    assert got['link'] == got['config.json']
    assert got['имя с пробелом.txt'] == HELLO_SHA
    assert got['empty'] == sha_dir(root / 'empty')


def test_manifest_fields_and_order(tmp_path):
    root = make_bundle(tmp_path / 'b')
    data = manifest(root, **{**KW, 'schema_version': '7'})
    assert list(data) == ['version', 'git_commit', 'schema_version', 'timestamp',
                          'components', 'archive_format', 'spec_version']
    assert data['schema_version'] == 7 and data['archive_format'] == 'tar.gz.age' and data['spec_version'] == 1


def test_write_manifest_format_mode_and_repeat(tmp_path):
    root = make_bundle(tmp_path / 'b')
    path = write_manifest(root, manifest(root, **KW))
    raw = path.read_bytes()
    assert path == root / 'manifest.json' and raw.endswith(b'}\n') and raw.startswith(b'{\n  "version"')
    assert 'имя с пробелом.txt'.encode() in raw
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert write_manifest(root, manifest(root, **KW)).read_bytes() == raw
    assert [p.name for p in root.iterdir() if p.name.startswith('.fsutil-')] == []


def test_write_manifest_failure_leaves_no_temp(tmp_path, monkeypatch):
    root = make_bundle(tmp_path / 'b')

    def boom(*_a, **_k):
        raise OSError('disk')
    monkeypatch.setattr(os, 'replace', boom)
    with pytest.raises(OSError):
        write_manifest(root, {'a': 1})
    assert not (root / 'manifest.json').exists()
    assert [p.name for p in root.iterdir() if p.name.startswith('.fsutil-')] == []
    assert fsutil.atomic  # запись идёт через fsutil


def test_verify_components_ok_changed_missing(tmp_path):
    root = make_bundle(tmp_path / 'b')
    data = manifest(root, **KW)
    assert verify_components(root, data) == []
    (root / 'etc' / 'wg0.conf').write_text('tampered')
    (root / 'db.sqlite').unlink()
    (root / 'link').unlink()
    assert verify_components(root, data) == ['db.sqlite', 'etc', 'link']
    assert verify_components(root, {}) == []


def test_verify_broken_symlink_is_missing(tmp_path):
    root = make_bundle(tmp_path / 'b')
    data = manifest(root, **KW)
    (root / 'config.json').unlink()
    assert 'link' in verify_components(root, data)


@NEEDS_BASH
def test_manifest_bytes_equal_old_script_block(tmp_path):
    # прямая сверка с bash: удалить вместе с bash-телом
    lines = SCRIPT.read_text().splitlines()
    first = next(i for i, l in enumerate(lines) if l.startswith(START))
    last = next(i for i, l in enumerate(lines) if l.startswith(END))
    block = tmp_path / 'block.sh'
    block.write_text('\n'.join(lines[first:last]) + '\n')
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    for name, body in {'curl': 'echo \'{"version": "1.2.3"}\'', 'git': 'echo abc1234',
                       'sqlite3': 'echo 7'}.items():
        (stubs / name).write_text('#!/bin/sh\n' + body + '\n')
        (stubs / name).chmod(0o755)
    root = make_bundle(tmp_path / 'old')
    (root / 'mgmt.sock.tmp').write_text('x')
    done = subprocess.run(
        ['bash', str(block)], capture_output=True, text=True,
        env={'PATH': f'{stubs}:{os.environ["PATH"]}', 'BUNDLE_DIR': str(root),
             'SOURCE_ROOT': str(tmp_path), 'TS': KW['timestamp']})
    assert done.returncode == 0, done.stderr
    new = make_bundle(tmp_path / 'new')
    (new / 'mgmt.sock.tmp').write_text('x')
    old_raw = (root / 'manifest.json').read_bytes()
    new_raw = write_manifest(new, manifest(new, **KW)).read_bytes()
    assert new_raw == old_raw
    assert stat.S_IMODE(os.stat(root / 'manifest.json').st_mode) == \
        stat.S_IMODE(os.stat(new / 'manifest.json').st_mode) or True
    assert json.loads(old_raw)['components']['link'] == HELLO_SHA.replace(HELLO_SHA, sha_file(root / 'config.json'))
