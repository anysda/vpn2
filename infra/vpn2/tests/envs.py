"""Значения узла из config.yaml так, как деплой отдаёт их ролям: `inventory.py --prepare`, затем `--env`."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
CONFIG = (ROOT / 'config.example.yaml').read_text().replace('  passphrase: ""', '  passphrase: test pass')


def inventory(directory, *args, env=None):
    directory = Path(directory)
    environ = {**os.environ, **(env or {}), 'PYTHONPATH': str(ROOT / 'infra/vpn2/src'),
               'VPN2_CONFIG': str(directory / 'config.yaml')}
    environ.setdefault('VPN2_STATE_DIR', str(directory / 'state'))
    return subprocess.run([sys.executable, '-B', str(ROOT / 'infra/ansible/inventory.py'), *args],
                          capture_output=True, text=True, env=environ, timeout=30)


def node_env(directory, text=CONFIG, node='ru', env=None):
    """(результат --prepare, {имя: значение} узла или None при отказе) для config.yaml `text`."""
    (Path(directory) / 'config.yaml').write_text(text)
    prepared = inventory(directory, '--prepare', env=env)
    if prepared.returncode:
        return prepared, None
    out = inventory(directory, '--env', node, env=env).stdout
    return prepared, dict(pair.split('=', 1) for pair in shlex.split(out))
