"""vpn2-apply на узле - вход в пакет: тот же разбор wg0.conf, что у службы."""
import subprocess
import sys
from pathlib import Path

from vpn2 import wg
from vpn2.control import apply

INFRA = Path(__file__).resolve().parents[2]
SRC = INFRA / 'vpn2/src'


def test_apply_reads_wg_through_the_package():
    assert apply.wg is wg
    assert not hasattr(apply, 'wg_line') and not hasattr(apply, 'WG_SECTIONS')


def test_launcher_runs_the_package_command(tmp_path):
    """Файл, который стадия кладёт в /usr/local/sbin/vpn2-apply, зовёт `vpn2 control apply`."""
    text = (SRC / 'vpn2/control/apply_launcher.py').read_text(encoding='utf-8')
    assert text.startswith('#!/usr/bin/python3 -I\n')
    launcher = tmp_path / 'vpn2-apply'
    launcher.write_text(text.replace('/usr/local/lib/vpn2-control', str(SRC)), encoding='utf-8')
    ran = subprocess.run([sys.executable, '-I', str(launcher), 'mark', 'nothing'],
                         capture_output=True, text=True, timeout=60)
    assert ran.returncode == 2
    assert "invalid choice: 'nothing'" in ran.stderr


def test_stage_installs_the_launcher():
    stage = INFRA / 'scripts/29-control.sh'
    if stage.exists():
        assert 'control/apply_launcher.py" /usr/local/sbin/.vpn2-apply.new' in stage.read_text(encoding='utf-8')
