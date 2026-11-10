"""infra/lib/ansible-venv.sh: хэш пинов и сборка колеса vpn2 без сети.

Хэш пинов меняется от правки каждого входящего файла и не меняется от
__pycache__. Колесо собирается в VPN2_ROOT, дерево репы остаётся чистым,
повтор при тех же исходниках не пересобирает, правка исходника пересобирает.
pip и galaxy здесь не зовутся.
"""
import glob
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'infra/lib/ansible-venv.sh'
PIN_FILES = ('versions.yml', 'ansible/constraints.txt', 'ansible/requirements.yml',
             'vpn2/pyproject.toml', 'vpn2/src/vpn2/config.py')
NEEDS_WHEEL = pytest.mark.skipif(
    not glob.glob('/usr/share/python-wheels/setuptools-*.whl'),
    reason='нет системного setuptools-*.whl (python3-venv)')


def bash(body, **env):
    """Запуск функции lib в bash; log и die даёт вызывающий."""
    script = ('log() { echo "$*"; }; die() { echo "$*" >&2; exit 1; }\n'
              f'source {LIB}\n{body}\n')
    return subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                          env={**os.environ, **env})


@pytest.fixture
def infra(tmp_path):
    """Копия входящих файлов infra: правка не трогает настоящее дерево."""
    dst = tmp_path / 'infra'
    shutil.copytree(ROOT / 'infra/vpn2', dst / 'vpn2',
                    ignore=shutil.ignore_patterns('__pycache__', 'tests', '*.egg-info'))
    for name in PIN_FILES:
        (dst / name).parent.mkdir(parents=True, exist_ok=True)
        src = ROOT / 'infra' / name
        if src.exists():
            shutil.copy(src, dst / name)
        else:
            (dst / name).write_text('x\n')
    return dst


def pins(infra_dir):
    res = bash('ansible_venv_pins', ANSIBLE_VENV_INFRA=str(infra_dir))
    assert res.returncode == 0, res.stderr
    assert len(res.stdout.strip()) == 64
    return res.stdout.strip()


@pytest.mark.parametrize('name', PIN_FILES)
def test_pins_change_with_each_input(infra, name):
    before = pins(infra)
    with open(infra / name, 'a') as f:
        f.write('\n# правка\n')
    assert pins(infra) != before


def test_pins_ignore_pycache(infra):
    before = pins(infra)
    cache = infra / 'vpn2/src/vpn2/__pycache__'
    cache.mkdir()
    (cache / 'config.cpython-314.pyc').write_bytes(b'junk')
    (infra / 'vpn2/src/vpn2/stale.pyc').write_bytes(b'junk')
    assert pins(infra) == before


def build(tmp_path, infra_dir=None):
    env = {'VPN2_ROOT': str(tmp_path / 'root')}
    if infra_dir:
        env['ANSIBLE_VENV_INFRA'] = str(infra_dir)
    res = bash('ansible_wheel_build', **env)
    assert res.returncode == 0, res.stderr
    return tmp_path / 'root/wheels/vpn2.whl'


def tree_status():
    res = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain', 'infra/vpn2'],
                         capture_output=True, text=True)
    return res.stdout


@NEEDS_WHEEL
def test_wheel_build_leaves_tree_clean(tmp_path):
    # Сборка ничего не добавляет к тому, что уже было в дереве до неё.
    before = tree_status()
    wheel = build(tmp_path)
    assert wheel.stat().st_size > 0
    assert (wheel.parent / '.src-sha256').is_file()
    assert tree_status() == before


@NEEDS_WHEEL
def test_second_build_does_not_rebuild(tmp_path):
    wheel = build(tmp_path)
    first = wheel.stat().st_mtime_ns
    build(tmp_path)
    assert wheel.stat().st_mtime_ns == first


@NEEDS_WHEEL
def test_source_edit_rebuilds(tmp_path, infra):
    wheel = build(tmp_path, infra)
    first = wheel.stat().st_mtime_ns
    with open(infra / 'vpn2/src/vpn2/config.py', 'a') as f:
        f.write('\n# правка\n')
    build(tmp_path, infra)
    assert wheel.stat().st_mtime_ns != first
