"""venv деплоя: пины, колесо vpn2 без сети, сборка рядом с действующей, уборка старых."""
import subprocess
import time
from pathlib import Path

import pytest

from vpn2 import deploy

# Заглушка системного python3: venv, pip из whl и сборка колеса пишут шаг в FAKE_LOG.
PYTHON = '''#!/bin/bash
log() { echo "$*" >> "$FAKE_LOG"; }
case "$1" in
  -V) echo 'Python 3.99.0' ;;
  -I) log build; touch "$6/vpn2-0.1.0-py3-none-any.whl" ;;
  -m) log venv; mkdir -p "$4/bin"; printf '#!/bin/sh\\necho galaxy >> "$FAKE_LOG"\\n' > "$4/bin/ansible-galaxy"
      chmod +x "$4/bin/ansible-galaxy" ;;
  */pip) case " $* " in
      *' download '*) log download ;;
      *' --no-deps '*) log install-wheel ;;
      *) log install-ansible; [ -z "$FAKE_FAIL" ] || exit 1 ;;
    esac ;;
esac
'''


@pytest.fixture
def box(tmp_path, monkeypatch):
    infra, wheels = tmp_path / 'infra', tmp_path / 'wheels'
    for path, text in (('ansible/constraints.txt', 'ansible-core==2.0\n'), ('ansible/requirements.yml', 'collections: []\n'),
                       ('vpn2/pyproject.toml', '[project]\n'), ('vpn2/src/vpn2/__init__.py', '')):
        (infra / path).parent.mkdir(parents=True, exist_ok=True)
        (infra / path).write_text(text)
    wheels.mkdir()
    for name in ('setuptools-80.0-py3-none-any.whl', 'pip-25.0-py3-none-any.whl'):
        (wheels / name).touch()
    (tmp_path / 'python').write_text(PYTHON)
    (tmp_path / 'python').chmod(0o755)
    for name, value in (('INFRA', infra), ('ROOT', tmp_path / 'opt'), ('WHEELS_DIR', wheels), ('PYTHON', str(tmp_path / 'python'))):
        monkeypatch.setattr(deploy, name, value)
    monkeypatch.setenv('FAKE_LOG', str(tmp_path / 'log'))
    monkeypatch.setattr(deploy.time, 'sleep', lambda _seconds: None)
    return tmp_path


def steps(box):
    log = box / 'log'
    found = log.read_text().split() if log.exists() else []
    log.unlink(missing_ok=True)
    return found


def test_pins_follow_python_constraints_and_requirements(box):
    first = deploy.venv_pins()
    assert deploy.venv_pins() == first
    (box / 'infra/ansible/requirements.yml').write_text('collections: [ansible.posix]\n')
    second = deploy.venv_pins()
    assert second != first
    (box / 'python').write_text(PYTHON.replace('3.99.0', '3.99.1'))
    assert deploy.venv_pins() != second


def test_source_hash_ignores_pycache(box):
    before = deploy.source_hash()
    (box / 'infra/vpn2/src/vpn2/__pycache__').mkdir()
    (box / 'infra/vpn2/src/vpn2/__pycache__/x.cpython-312.pyc').write_text('x')
    assert deploy.source_hash() == before
    (box / 'infra/vpn2/src/vpn2/__init__.py').write_text('# edit\n')
    assert deploy.source_hash() != before


def test_install_branches(box):
    deploy.ensure_venv()
    assert steps(box) == ['build', 'download', 'venv', 'install-ansible', 'install-wheel', 'galaxy']
    current = (box / 'opt/venv').resolve()
    deploy.ensure_venv()
    assert steps(box) == []
    assert 'ansible: пины те же, без загрузки' in (box / 'opt/ansible-venv.log').read_text()
    # Правка исходников: новое колесо и сборка, без загрузки и коллекций; старая сборка убрана.
    (box / 'infra/vpn2/src/vpn2/__init__.py').write_text('# edit\n')
    deploy.ensure_venv()
    assert steps(box) == ['build', 'venv', 'install-ansible', 'install-wheel']
    assert 'колесо vpn2 обновлено' in (box / 'opt/ansible-venv.log').read_text()
    assert (box / 'opt/venv').resolve() != current and not current.exists()
    # Смена пинов: загрузка и коллекции заново.
    (box / 'infra/ansible/constraints.txt').write_text('ansible-core==2.1\n')
    deploy.ensure_venv()
    assert steps(box) == ['download', 'venv', 'install-ansible', 'install-wheel', 'galaxy']


def test_aborted_build_leaves_current_venv_working(box, monkeypatch):
    deploy.ensure_venv()
    current = (box / 'opt/venv').resolve()
    steps(box)
    (box / 'infra/ansible/constraints.txt').write_text('ansible-core==2.1\n')
    monkeypatch.setenv('FAKE_FAIL', '1')
    with pytest.raises(SystemExit):
        deploy.ensure_venv()
    assert (box / 'opt/venv').resolve() == current and (current / '.vpn2-pins').exists()
    monkeypatch.delenv('FAKE_FAIL')
    steps(box)
    deploy.ensure_venv()
    # Оборванная сборка без .vpn2-pins собирается заново, склад колёс уже под новые пины.
    assert steps(box) == ['venv', 'install-ansible', 'install-wheel', 'galaxy']


def test_build_of_running_process_is_kept(box):
    deploy.ensure_venv()
    old = (box / 'opt/venv').resolve()
    runner = subprocess.Popen(['bash', '-c', f'exec -a {old}/bin/python sleep 30'])
    try:
        time.sleep(0.2)
        (box / 'infra/vpn2/src/vpn2/__init__.py').write_text('# edit\n')
        deploy.ensure_venv()
        assert old.exists() and (box / 'opt/venv').resolve() != old
    finally:
        runner.kill()
        runner.wait()
    deploy.prune_venvs()
    assert not old.exists()


def test_missing_system_wheels_are_named(box):
    for path in (box / 'wheels').iterdir():
        path.unlink()
    with pytest.raises(SystemExit):
        deploy.ensure_venv()
    assert f'нет setuptools-*.whl в {box}/wheels' in (box / 'opt/ansible-venv.log').read_text()


def test_wheel_path_is_the_built_wheel(box):
    assert deploy.wheel_path() is None
    deploy.ensure_venv()
    assert deploy.wheel_path() == Path(box / 'opt/wheels/vpn2-0.1.0-py3-none-any.whl')


def test_missing_command_is_installed_by_package_name(monkeypatch, capsys):
    """На entry проверяются команды, apt получает имена пакетов: ssh - это openssh-client."""
    asked = []
    monkeypatch.setattr(deploy.shutil, 'which', lambda name: None if name in ('ssh', 'sshpass') else f'/usr/bin/{name}')
    monkeypatch.setattr(deploy, 'apt_have', lambda *_packages: True)
    monkeypatch.setattr(deploy, 'svc', lambda _script, *packages, **_options: asked.extend(packages) or 0)
    monkeypatch.setattr(deploy, 'ensure_venv', lambda: None)
    monkeypatch.setattr(deploy, 'wheel_path', lambda: 'vpn2.whl')
    monkeypatch.setattr(deploy.Path, 'is_file', lambda path: str(path) == '/etc/debian_version' or Path.exists(path))
    deploy.install_prereqs()
    assert asked == ['openssh-client', 'sshpass'] and 'ставлю недостающее: ssh sshpass' in capsys.readouterr().out
