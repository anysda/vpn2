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
PIN_FILES = ('ansible/constraints.txt', 'ansible/requirements.yml')
OTHER_FILES = ('versions.yml', 'vpn2/pyproject.toml', 'vpn2/src/vpn2/config.py')
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
    for name in PIN_FILES + OTHER_FILES:
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


@pytest.mark.parametrize('name', OTHER_FILES)
def test_pins_ignore_versions_and_package_sources(infra, name):
    before = pins(infra)
    with open(infra / name, 'a') as f:
        f.write('\n# правка\n')
    assert pins(infra) == before


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
    wheels = sorted((tmp_path / 'root/wheels').glob('vpn2-*-py3-none-any.whl'))
    assert len(wheels) == 1, wheels
    return wheels[0]


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
    wheel = build(tmp_path, infra)  # в каталоге по-прежнему одно колесо
    assert wheel.stat().st_mtime_ns != first


@NEEDS_WHEEL
def test_wheel_path(tmp_path):
    env = {'VPN2_ROOT': str(tmp_path / 'root')}
    res = bash('ansible_wheel_path', **env)
    assert res.returncode == 1 and res.stdout == ''
    wheel = build(tmp_path)
    res = bash('ansible_wheel_path', **env)
    assert res.returncode == 0 and res.stdout.strip() == str(wheel)
    assert wheel.name.startswith('vpn2-') and wheel.name.endswith('-py3-none-any.whl')
    assert not (wheel.parent / 'vpn2.whl').exists()
    assert not (wheel.parent / '.name').exists()


@pytest.fixture
def fake_python(tmp_path):
    """Заглушка python3 в PATH: пишет аргументы в файл, pip и сеть не зовутся."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    calls = tmp_path / 'calls'
    stub = bin_dir / 'python3'
    stub.write_text('#!/bin/sh\n'
                    'if [ "$1" = "-V" ]; then echo "Python 3.0.0"; exit 0; fi\n'
                    f'echo "$*" >> {calls}\n')
    stub.chmod(0o755)
    return bin_dir, calls


def install(tmp_path, bin_dir, infra_dir):
    return bash('ansible_venv_install',
               VPN2_ROOT=str(tmp_path / 'root'), ANSIBLE_VENV_INFRA=str(infra_dir),
               PATH=f'{bin_dir}:{os.environ["PATH"]}')


@NEEDS_WHEEL
@pytest.mark.skipif(not glob.glob('/usr/share/python-wheels/pip-*.whl'),
                    reason='нет системного pip-*.whl (python3-venv)')
def test_install_branches(tmp_path, infra, fake_python):
    bin_dir, calls = fake_python
    build(tmp_path, infra)
    root = tmp_path / 'root'
    venv = root / 'venv'
    venv.mkdir(parents=True)
    pin = bash('ansible_venv_pins', ANSIBLE_VENV_INFRA=str(infra),
               PATH=f'{bin_dir}:{os.environ["PATH"]}').stdout.strip()
    (venv / '.vpn2-pins').write_text(pin + '\n')
    (venv / '.vpn2-wheel').write_text((root / 'wheels/.src-sha256').read_text())
    # тот же маркер колеса: ни одного вызова
    res = install(tmp_path, bin_dir, infra)
    assert res.returncode == 0, res.stderr
    assert 'пины те же, без загрузки' in res.stdout
    assert not calls.exists()
    # другое колесо: один вызов с --no-index
    with open(infra / 'vpn2/src/vpn2/config.py', 'a') as f:
        f.write('\n# правка\n')
    build(tmp_path, infra)
    res = install(tmp_path, bin_dir, infra)
    assert res.returncode == 0, res.stderr
    assert 'колесо vpn2 обновлено' in res.stdout
    lines = calls.read_text().splitlines()
    assert len(lines) == 1 and '--no-index' in lines[0] and '--force-reinstall' in lines[0]
    assert (venv / '.vpn2-wheel').read_text() == (root / 'wheels/.src-sha256').read_text()


def test_background_install_does_not_hold_deploy_lock(tmp_path):
    # Деплой берёт замок через exec {fd}> и падает; фон с sleep ещё идёт,
    # а следующий запуск обязан взять тот же замок.
    lock = tmp_path / 'deploy.lock'
    res = bash(
        f'exec {{fd}}>{lock}; flock -n "$fd" || exit 9\n'
        'ansible_wheel_build() { :; }\n'
        '_ansible_venv_install_locked() { sleep 20; }\n'
        'ansible_venv_start\n'
        'echo "bg=$_VENV_PID" >&2\n'
        'exit 1',
        VPN2_ROOT=str(tmp_path / 'root'))
    bg = int(res.stderr.rsplit('bg=', 1)[1].split()[0])
    try:
        assert res.returncode == 1, res.stderr
        second = subprocess.run(['flock', '-n', str(lock), 'true'])
        assert second.returncode == 0, 'фон venv держит замок деплоя'
    finally:
        subprocess.run(['pkill', '-P', str(bg)])
        subprocess.run(['kill', str(bg)])


def venv_wait(tmp_path, mode):
    log = tmp_path / 'venv.log'
    return bash(
        'warn() { echo "WARN $*" >&2; }\n'
        f'echo pip-boom > {log}; (exit 1) & _VENV_PID=$!\n'
        f'ansible_venv_wait {mode}; echo "rc=$?"',
        VPN2_ROOT=str(tmp_path / 'root'), VENV_LOG=str(log))


def test_soft_venv_wait_warns_and_returns_zero(tmp_path):
    res = venv_wait(tmp_path, 'soft')
    assert res.returncode == 0, res.stderr
    assert 'rc=0' in res.stdout
    assert 'pip-boom' in res.stderr
    assert 'WARN ansible: venv не установился, лог' in res.stderr
    assert 'повторится при следующем запуске' in res.stderr


def test_strict_venv_wait_dies_on_failed_background(tmp_path):
    res = venv_wait(tmp_path, '')
    assert res.returncode != 0
    assert 'rc=' not in res.stdout
    assert 'ansible: venv не установился' in res.stderr




@NEEDS_WHEEL
@pytest.mark.parametrize('ready', (False, True))
def test_wheel_build_installs_venv_package_only_when_setuptools_missing(tmp_path, ready):
    system = glob.glob('/usr/share/python-wheels/setuptools-*.whl')[0]
    wheels = tmp_path / 'pywheels'
    wheels.mkdir()
    if ready:
        shutil.copy(system, wheels)
    calls = tmp_path / 'calls'
    res = bash(
        f'_install_prereq_pkgs() {{ echo "$*" >> {calls}; cp {system} "$PYTHON_WHEELS_DIR/"; }}\n'
        'ansible_wheel_build',
        VPN2_ROOT=str(tmp_path / 'root'), PYTHON_WHEELS_DIR=str(wheels))
    assert res.returncode == 0, res.stderr
    assert list((tmp_path / 'root/wheels').glob('vpn2-*-py3-none-any.whl'))
    if ready:
        assert not calls.exists()
    else:
        assert calls.read_text().splitlines() == ['python3-venv']
