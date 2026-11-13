"""Сторож фаервола на узле: снимок до стадии, откат при тишине новых ssh-входов, снятие."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / 'src/vpn2/fwguard.py'
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


def environ(node):
    return {**os.environ, 'PATH': f'{node / "bin"}:{os.environ["PATH"]}', 'STATE': str(node / 'state'),
            'FW_GUARD_DIR': str(node / 'run'), 'FW_GUARD_ETC': str(node / 'etc')}


def guard(node, *args):
    return subprocess.run([sys.executable, '-I', GUARD, *args], capture_output=True, text=True, env=environ(node), timeout=30)


def break_firewall(node):
    """Стадия закрыла ssh: правила ufw другие, ufw выключен, таблица и её файл новые."""
    (node / 'etc/ufw/user.rules').write_text('deny 22\n')
    (node / 'etc/ufw/extra.rules').write_text('extra\n')
    (node / 'state/active').unlink()
    (node / 'state/table-vpn2_hy2hop').write_text('table inet vpn2_hy2hop { broken }\n')
    (node / 'state/table-ytzapret').write_text('table inet ytzapret { new }\n')
    (node / 'etc/nftables-hy2hop.conf').write_text('broken\n')


def test_arm_starts_the_watch_from_its_own_copy(node):
    done = guard(node, 'arm', '10-foreign', '90')
    assert done.returncode == 0, done.stderr
    started = (node / 'state/calls').read_text()
    assert f'--unit=vpn2-fw-guard --setenv=FW_GUARD_DIR={node}/run' in started
    assert f'{node}/run/fwguard.py watch 90' in started
    assert (node / 'run/stage').read_text() == '10-foreign\n' and (node / 'run/snap/ufw-active').exists()


def test_silence_longer_than_the_window_restores_the_snapshot(node):
    assert guard(node, 'arm', '20-ru-router', '1').returncode == 0
    break_firewall(node)
    os.utime(node / 'run/alive', (time.time() - 10, time.time() - 10))
    done = guard(node, 'watch', '1')
    assert done.returncode == 0, done.stdout + done.stderr
    assert 'откатываю фаервол к снимку до стадии 20-ru-router' in done.stdout
    assert (node / 'etc/ufw/user.rules').read_text() == 'allow 22\n' and not (node / 'etc/ufw/extra.rules').exists()
    assert (node / 'state/active').exists() and 'reload' in (node / 'state/calls').read_text()
    assert (node / 'state/table-vpn2_hy2hop').read_text() == 'table inet vpn2_hy2hop { }\n'
    assert not (node / 'state/table-ytzapret').exists()
    assert (node / 'etc/nftables-hy2hop.conf').read_text() == 'hop\n'
    late = guard(node, 'disarm', '20-ru-router')
    assert late.returncode == 3 and 'ufw и таблицы стадий откатаны к состоянию до неё' in late.stdout
    assert not (node / 'run').exists()


def test_disarm_in_time_keeps_the_stage_firewall(node):
    assert guard(node, 'arm', '27-ikev2', '90').returncode == 0
    break_firewall(node)
    done = guard(node, 'disarm', '27-ikev2')
    assert done.returncode == 0 and not (node / 'run').exists()
    assert (node / 'etc/ufw/user.rules').read_text() == 'deny 22\n'
    # Снятый сторож решения не принимает.
    assert guard(node, 'watch', '0').returncode == 0
    assert (node / 'etc/ufw/user.rules').read_text() == 'deny 22\n'


def test_disarm_without_guard_is_reported(node):
    done = guard(node, 'disarm', '30-frontend')
    assert done.returncode == 4 and 'сторож стадии 30-frontend не найден' in done.stdout


def test_beats_keep_the_firewall(node):
    assert guard(node, 'arm', '05-mgmt-mesh', '3').returncode == 0
    break_firewall(node)
    watch = subprocess.Popen([sys.executable, '-I', GUARD, 'watch', '3'], env=environ(node))
    for _ in range(4):
        time.sleep(2)
        subprocess.run(['touch', '-c', node / 'run/alive'], check=True)  # пульс из deploy.yml
    assert guard(node, 'disarm', '05-mgmt-mesh').returncode == 0
    assert watch.wait(timeout=10) == 0
    assert (node / 'etc/ufw/user.rules').read_text() == 'deny 22\n'
