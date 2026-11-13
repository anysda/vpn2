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


def parse(text):
    return dict(pair.split('=', 1) for pair in shlex.split(text))


def node_envs(directory, text=CONFIG, env=None):
    """(результат --prepare, {узел: вывод --env}) для ru и каждого экзита; {} при отказе."""
    (Path(directory) / 'config.yaml').write_text(text)
    prepared = inventory(directory, '--prepare', env=env)
    if prepared.returncode:
        return prepared, {}
    ru = inventory(directory, '--env', 'ru', env=env).stdout
    tags = parse(ru).get('EXIT_TAGS', '').split()
    return prepared, {'ru': ru, **{tag: inventory(directory, '--env', tag, env=env).stdout for tag in tags}}


def node_env(directory, text=CONFIG, node='ru', env=None):
    """(результат --prepare, {имя: значение} узла или None при отказе) для config.yaml `text`."""
    prepared, texts = node_envs(directory, text, env)
    return prepared, parse(texts[node]) if texts else None


def sourced(directory, text, script):
    """Вывод --env узла через `source` в bash: значения доходят до shell целыми и без исполнения."""
    (Path(directory) / 'node.env').write_text(text)
    return subprocess.run(['bash', '-euc', f'set -a; source node.env; {script}'], cwd=directory,
                          capture_output=True, text=True, timeout=5)
