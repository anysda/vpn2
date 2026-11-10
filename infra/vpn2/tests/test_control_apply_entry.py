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


def test_wrapper_runs_the_package_command():
    """Обёртка /usr/local/sbin/vpn2-apply из роли control зовёт `vpn2 control apply`."""
    tasks = (INFRA / 'ansible/roles/control/tasks/main.yml').read_text(encoding='utf-8')
    assert 'exec {{ control_vpn2 }} control apply "$@"\n    dest: /usr/local/sbin/vpn2-apply\n' in tasks
    ran = subprocess.run([sys.executable, '-c', 'import sys; from vpn2.cli import main; sys.exit(main())',
                          'control', 'apply', 'mark', 'nothing'],
                         env={'PYTHONPATH': str(SRC), 'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=60)
    assert ran.returncode == 2
    assert "invalid choice: 'nothing'" in ran.stderr
