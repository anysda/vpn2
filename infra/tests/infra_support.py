"""Общие помощники тестов инфры: файлы дерева, bash и значения узлов из config.yaml в infra/envs."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))


def source(path):
    return (ROOT / path).read_text()


def bash(script, directory):
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                          cwd=directory, text=True, capture_output=True, timeout=15)


def write_envs(directory, env=None):
    """infra/envs/<узел>.env, all.env и exits.env из config.yaml каталога: значения узлов, что деплой отдаёт ролям."""
    root = Path(directory)
    environ = {**os.environ, **(env or {}), 'PYTHONPATH': str(ROOT / 'infra/vpn2/src'),
               'VPN2_CONFIG': str(root / 'config.yaml')}
    environ.setdefault('VPN2_STATE_DIR', str(root / 'state'))

    def inventory(*args):
        return subprocess.run([sys.executable, '-B', str(ROOT / 'infra/ansible/inventory.py'), *args],
                              capture_output=True, text=True, env=environ, timeout=30)

    result = inventory('--prepare')
    if result.returncode:
        return result
    envs = root / 'infra/envs'
    envs.mkdir(parents=True, exist_ok=True)
    ru = inventory('--env', 'ru').stdout
    tags = values_of(ru).get('EXIT_TAGS', '').split()
    for node in ['ru', *tags]:
        (envs / f'{node}.env').write_text(ru if node == 'ru' else inventory('--env', node).stdout)
    (envs / 'all.env').write_text(ru)
    (envs / 'exits.env').write_text(f"EXIT_TAGS={shlex.quote(' '.join(tags))}\n")
    return result


def values_of(text):
    return dict(pair.split('=', 1) for pair in shlex.split(text))


def values(directory, node='ru'):
    """Значения узла из infra/envs/<узел>.env после write_envs."""
    return values_of((Path(directory) / f'infra/envs/{node}.env').read_text())
