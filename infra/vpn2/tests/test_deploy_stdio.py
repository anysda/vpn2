"""Цепочки entry и экзитов делят вывод деплоя: ssh сторожа одной не мешает ansible-playbook другой."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'infra/tests'))
import vpn_roles  # noqa: E402

# Как OpenSSH без tty: O_NONBLOCK на свой stdout на время входа; вход держится, пока не сыгран плей.
SSH = r'''#!PYTHON
import os, sys, time
from pathlib import Path
work = Path(os.environ['FAKE_WORK'])
os.set_blocking(1, False)
print('vpn2-fw-guard: сторож снят', flush=True)
(work / 'ssh-up').touch()
for _ in range(300):
    if (work / 'played').exists():
        break
    time.sleep(0.1)
'''
# Цепочка экзитов снимает сторожа 23-emergency, entry в это время начинает следующую стадию.
CHAINS = r'''import os, sys, threading, time
from pathlib import Path
from vpn2.deploy import run
work = Path(os.environ['FAKE_WORK'])
deploy = run.Deploy.__new__(run.Deploy)
deploy.logins, deploy.children, deploy.closing = {'nl': ('nl', '', '')}, set(), False
deploy.lock, deploy.beat, deploy.beats = threading.Lock(), None, 1
side = threading.Thread(target=deploy.guard_finish, args=('23-emergency', 'nl'))
side.start()
while not (work / 'ssh-up').exists():
    time.sleep(0.05)
if sys.argv[1] == 'shared':
    os.set_blocking(1, False)
code = deploy.play('--version')
(work / 'played').touch()
side.join()
os.set_blocking(1, True)
print(f'play={code}', flush=True)
'''


@pytest.mark.parametrize('mode, refused', [('ssh', False), ('shared', True)])
def test_guard_ssh_leaves_shared_output_blocking(tmp_path, mode, refused):
    """shared - положительный контроль: неблокирующий общий вывод настоящий ansible-playbook отвергает."""
    venv, fakes = tmp_path / 'root/venv/bin', tmp_path / 'bin'
    venv.mkdir(parents=True)
    fakes.mkdir()
    (venv / 'ansible-playbook').symlink_to(vpn_roles.ansible_playbook())
    (fakes / 'ssh').write_text(SSH.replace('PYTHON', sys.executable))
    (fakes / 'ssh').chmod(0o755)
    env = {**os.environ, 'VPN2_ROOT': str(tmp_path / 'root'), 'FAKE_WORK': str(tmp_path), 'PYTHONPATH': str(ROOT / 'infra/vpn2/src'),
           'PATH': f"{fakes}:{os.environ['PATH']}", 'VPN2_FW_GUARD_WINDOW': '5'}
    with open(tmp_path / 'deploy.log', 'w') as out:
        subprocess.run([sys.executable, '-B', '-c', CHAINS, mode], stdin=subprocess.DEVNULL, stdout=out,
                       stderr=subprocess.STDOUT, env=env, timeout=120, check=True)
    said = (tmp_path / 'deploy.log').read_text()
    assert ('Ansible requires blocking IO' in said) is refused, said
    assert said.endswith('play=1\n' if refused else 'play=0\n'), said
    assert 'vpn2-fw-guard: сторож снят' in said
