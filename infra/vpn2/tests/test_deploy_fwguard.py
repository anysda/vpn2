"""Сторож фаервола на узле: снимок до стадии, откат при тишине новых ssh-входов, снятие."""
import os
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from vpn2 import fwguard

# ufw и nft - заглушки над каталогом STATE: включённый ufw - файл active, таблица - файл table-<имя>.
FAKES = {
    'ufw': '''case "$1 $2" in
  'status '*) [ -e "$STATE/active" ] && echo 'Status: active' || echo 'Status: inactive' ;;
  '--force enable') touch "$STATE/active" ;;
  '--force disable') rm -f "$STATE/active" ;;
  'reload '*) echo reload >> "$STATE/calls" ;;
esac''',
    'nft': '''case "$1" in
  list) cat "$STATE/table-$4" 2>/dev/null ;;
  delete) rm -f "$STATE/table-$4" ;;
  -f) cp "$2" "$STATE/table-$(sed -n 's/^table inet \\([a-z0-9_]*\\).*/\\1/p' "$2")" ;;
esac''',
    'systemctl': 'echo "systemctl $*" >> "$STATE/calls"',
    'systemd-run': 'echo "systemd-run $*" >> "$STATE/calls"',
}


@pytest.fixture
def node(tmp_path):
    bin_dir, state, etc = tmp_path / 'bin', tmp_path / 'state', tmp_path / 'etc'
    for folder in (bin_dir, state, etc / 'ufw', etc / 'default'):
        folder.mkdir(parents=True)
    for name, body in FAKES.items():
        (bin_dir / name).write_text(f'#!/bin/sh\n{body}\n')
        (bin_dir / name).chmod(0o755)
    (etc / 'ufw/user.rules').write_text('allow 22\n')
    (etc / 'default/ufw').write_text('IPV6=yes\n')
    (etc / 'nftables-hy2hop.conf').write_text('hop\n')
    (state / 'table-vpn2_hy2hop').write_text('table inet vpn2_hy2hop { }\n')
    (state / 'active').touch()
    return tmp_path


@pytest.fixture
def guard(node, monkeypatch, capsys):
    """fwguard.main в этом процессе: каталог сторожа и /etc - в node, ufw/nft/systemd - заглушки из PATH."""
    monkeypatch.setenv('PATH', f'{node / "bin"}:{os.environ["PATH"]}')
    monkeypatch.setenv('STATE', str(node / 'state'))
    monkeypatch.setattr(fwguard, 'DIR', node / 'run')
    monkeypatch.setattr(fwguard, 'ETC', node / 'etc')

    def call(*args):
        capsys.readouterr()
        code = fwguard.main(list(args))
        out, err = capsys.readouterr()
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)
    return call


def break_firewall(node):
    """Стадия закрыла ssh: правила ufw другие, ufw выключен, таблица и её файл новые."""
    (node / 'etc/ufw/user.rules').write_text('deny 22\n')
    (node / 'etc/ufw/extra.rules').write_text('extra\n')
    (node / 'state/active').unlink()
    (node / 'state/table-vpn2_hy2hop').write_text('table inet vpn2_hy2hop { broken }\n')
    (node / 'state/table-ytzapret').write_text('table inet ytzapret { new }\n')
    (node / 'etc/nftables-hy2hop.conf').write_text('broken\n')


def test_arm_starts_the_watch_from_its_own_copy(node, guard):
    done = guard('arm', '10-foreign', '90')
    assert done.returncode == 0, done.stderr
    started = (node / 'state/calls').read_text()
    assert f'--unit=vpn2-fw-guard --setenv=FW_GUARD_DIR={node}/run' in started
    assert f'{node}/run/fwguard.py watch 90' in started
    assert (node / 'run/stage').read_text() == '10-foreign\n' and (node / 'run/snap/ufw-active').exists()


def test_silence_longer_than_the_window_restores_the_snapshot(node, guard):
    assert guard('arm', '20-ru-router', '1').returncode == 0
    break_firewall(node)
    os.utime(node / 'run/alive', (time.time() - 10, time.time() - 10))
    done = guard('watch', '1')
    assert done.returncode == 0, done.stdout + done.stderr
    assert 'откатываю фаервол к снимку до стадии 20-ru-router' in done.stdout
    assert (node / 'etc/ufw/user.rules').read_text() == 'allow 22\n' and not (node / 'etc/ufw/extra.rules').exists()
    assert (node / 'state/active').exists() and 'reload' in (node / 'state/calls').read_text()
    assert (node / 'state/table-vpn2_hy2hop').read_text() == 'table inet vpn2_hy2hop { }\n'
    assert not (node / 'state/table-ytzapret').exists()
    assert (node / 'etc/nftables-hy2hop.conf').read_text() == 'hop\n'
    late = guard('disarm', '20-ru-router')
    assert late.returncode == 3 and 'ufw и таблицы стадий откатаны к состоянию до неё' in late.stdout
    assert not (node / 'run').exists()


def test_disarm_in_time_keeps_the_stage_firewall(node, guard):
    assert guard('arm', '27-ikev2', '90').returncode == 0
    break_firewall(node)
    done = guard('disarm', '27-ikev2')
    assert done.returncode == 0 and not (node / 'run').exists()
    assert (node / 'etc/ufw/user.rules').read_text() == 'deny 22\n'
    # Снятый сторож решения не принимает.
    assert guard('watch', '0').returncode == 0
    assert (node / 'etc/ufw/user.rules').read_text() == 'deny 22\n'


def test_disarm_without_guard_is_reported(guard):
    done = guard('disarm', '30-frontend')
    assert done.returncode == 4 and 'сторож стадии 30-frontend не найден' in done.stdout


def test_beats_keep_the_firewall(node, guard):
    assert guard('arm', '05-mgmt-mesh', '3').returncode == 0
    break_firewall(node)
    watched = []
    watch = threading.Thread(target=lambda: watched.append(fwguard.watch(3)))
    watch.start()
    for _ in range(4):
        time.sleep(2)
        subprocess.run(['touch', '-c', node / 'run/alive'], check=True)  # пульс из deploy.yml
    assert guard('disarm', '05-mgmt-mesh').returncode == 0
    watch.join(timeout=10)
    assert watched == [0]
    assert (node / 'etc/ufw/user.rules').read_text() == 'deny 22\n'
