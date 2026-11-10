import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vpn2 import fsutil
from vpn2.fsutil import LockBusy, atomic, lock, read_json, write_json

SRC = str(Path(fsutil.__file__).resolve().parents[1])


def leftovers(directory):
    return sorted(p.name for p in directory.iterdir())


def test_atomic_writes_str_and_bytes(tmp_path):
    target = tmp_path / 'a'
    atomic(target, 'привет', encoding='utf-8')
    assert target.read_bytes() == 'привет'.encode()
    atomic(target, b'\x00\xff')
    assert target.read_bytes() == b'\x00\xff'
    assert leftovers(tmp_path) == ['a']


def test_atomic_mode_set_before_swap(tmp_path, monkeypatch):
    target = tmp_path / 'a'
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append(stat.S_IMODE(os.stat(src).st_mode))
        real(src, dst)

    monkeypatch.setattr(os, 'replace', spy)
    atomic(target, 'x', mode=0o640)
    assert seen == [0o640]
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@pytest.mark.parametrize('broken', ['fsync', 'replace'])
def test_atomic_failure_keeps_old_content_and_no_temp(tmp_path, monkeypatch, broken):
    target = tmp_path / 'a'
    atomic(target, 'old')

    def boom(*_args):
        raise OSError('disk gone')

    monkeypatch.setattr(os, broken, boom)
    with pytest.raises(OSError, match='disk gone'):
        atomic(target, 'new')
    monkeypatch.undo()
    assert target.read_text() == 'old'
    assert leftovers(tmp_path) == ['a']


def test_atomic_repeat_same_content_same_file_and_mode(tmp_path):
    target = tmp_path / 'a'
    atomic(target, 'same', mode=0o600)
    first = (target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
    atomic(target, 'same', mode=0o600)
    assert (target.read_bytes(), stat.S_IMODE(target.stat().st_mode)) == first
    assert leftovers(tmp_path) == ['a']


def test_atomic_changes_mode_of_existing_file(tmp_path):
    target = tmp_path / 'a'
    target.write_text('x')
    target.chmod(0o666)
    atomic(target, 'x', mode=0o640)
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_atomic_missing_directory_raises_without_creating(tmp_path):
    with pytest.raises(FileNotFoundError):
        atomic(tmp_path / 'nope' / 'a', 'x')
    assert not (tmp_path / 'nope').exists()


def test_atomic_symlink_is_replaced_by_file_target_untouched(tmp_path):
    real = tmp_path / 'real'
    real.write_text('target')
    link = tmp_path / 'link'
    link.symlink_to(real)
    atomic(link, 'new')
    assert not link.is_symlink()
    assert link.read_text() == 'new'
    assert real.read_text() == 'target'


def test_read_json_missing_default_and_without(tmp_path):
    missing = tmp_path / 'none.json'
    assert read_json(missing, default=None) is None
    assert read_json(missing, default={}) == {}
    with pytest.raises(FileNotFoundError):
        read_json(missing)


def test_read_json_broken_always_raises(tmp_path):
    broken = tmp_path / 'b.json'
    broken.write_text('{oops')
    with pytest.raises(json.JSONDecodeError):
        read_json(broken)
    with pytest.raises(json.JSONDecodeError):
        read_json(broken, default={})


def test_write_json_format_and_roundtrip_non_ascii(tmp_path):
    target = tmp_path / 'j.json'
    data = {'б': 'ключ', 'a': [1, 2], 'z': None}
    write_json(target, data)
    assert target.read_text(encoding='utf-8') == (
        '{\n  "a": [\n    1,\n    2\n  ],\n  "z": null,\n  "б": "ключ"\n}\n')
    assert read_json(target) == data
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    write_json(target, data, mode=0o644)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


CHILD = '''
import sys
sys.path.insert(0, sys.argv[1])
from vpn2.fsutil import LockBusy, lock
try:
    with lock(sys.argv[2], blocking=False):
        pass
except LockBusy:
    sys.exit(3)
'''


def try_in_other_process(path):
    return subprocess.run([sys.executable, '-c', CHILD, SRC, str(path)], check=False).returncode


def test_lock_between_processes(tmp_path):
    path = tmp_path / 'l'
    with lock(path):
        assert try_in_other_process(path) == 3
    assert try_in_other_process(path) == 0


def test_lock_nonblocking_busy_on_second_handle(tmp_path):
    path = tmp_path / 'l'
    with lock(path), pytest.raises(LockBusy), lock(path, blocking=False):
        pass


def test_lock_timeout_window(tmp_path):
    path = tmp_path / 'l'
    with lock(path):
        started = time.monotonic()
        with pytest.raises(LockBusy), lock(path, timeout=0.2):
            pass
        elapsed = time.monotonic() - started
    assert 0.2 <= elapsed <= 10


def test_lock_released_on_exception(tmp_path):
    path = tmp_path / 'l'
    with pytest.raises(ValueError, match='inside'), lock(path):
        raise ValueError('inside')
    assert try_in_other_process(path) == 0
    with lock(path, blocking=False):
        pass
