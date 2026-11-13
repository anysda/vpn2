"""Общие помощники тестов деплоя (договор ./deploy.sh - infra/vpn2/tests/test_deploy_cli.py).

VPN2_TEST_ROOT - другая ревизия под теми же проверками.
"""
import os
import shlex
import shutil
import sys
from pathlib import Path
import subprocess


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


def source(path):
    return (ROOT / path).read_text()


def function(name):
    """Функция bash по имени из оркестратора деплоя или его библиотек."""
    for path in [ROOT / 'infra/deploy.sh', *sorted((ROOT / 'infra/lib').glob('*.sh'))]:
        text = path.read_text()
        for start, end in ('{', '}'), ('(', ')'):
            if f'\n{name}() {start}' in text:
                return f'{name}() {start}' + text.split(f'\n{name}() {start}', 1)[1].split(f'\n{end}', 1)[0] + f'\n{end}\n'
    raise AssertionError(f'нет функции {name}')


def write_envs(directory, env=None):
    """infra/envs/<узел>.env, all.env и exits.env из config.yaml дерева: значения узлов, что деплой отдаёт ролям."""
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
    tags = next((shlex.split(line.partition('=')[2]) for line in ru.splitlines() if line.startswith('EXIT_TAGS=')), [''])
    tags = tags[0].split()
    for node in ['ru', *tags]:
        (envs / f'{node}.env').write_text(ru if node == 'ru' else inventory('--env', node).stdout)
    (envs / 'all.env').write_text(ru)
    (envs / 'exits.env').write_text(f"EXIT_TAGS={shlex.quote(' '.join(tags))}\n")
    return result


def values(directory, node='ru'):
    """Окружение узла из infra/envs/<узел>.env после write_envs: имя -> значение после разбора shell."""
    text = (Path(directory) / f'infra/envs/{node}.env').read_text()
    return dict(pair.split('=', 1) for pair in shlex.split(text))


def bash(script, directory):
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                          cwd=directory, text=True, capture_output=True, timeout=15)


CONFIG = (ROOT / 'config.example.yaml').read_text().replace('  passphrase: ""', '  passphrase: test pass')
TREE = ('deploy.sh', 'infra/deploy.sh', 'infra/lib', 'infra/ansible/site.yml', 'infra/ansible/deploy.yml',
        'infra/ansible/inventory.py', 'infra/vpn2/src/vpn2')


def deploy(directory, *args, config=CONFIG, without=()):
    """./deploy.sh копии дерева: состояние, замок и HOME во временном каталоге."""
    root = Path(directory)
    for name in TREE:
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
        else:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, root / name)
    if config is not None:
        (root / 'config.yaml').write_text(config)
    env = {**os.environ, 'HOME': str(root / 'home'), 'VPN2_STATE_DIR': str(root / 'state'),
           'VPN2_DEPLOY_LOCK': str(root / 'deploy.lock')}
    for name in without:
        env.pop(name, None)
    return subprocess.run(['bash', str(root / 'deploy.sh'), *args], input='', capture_output=True, text=True,
                          timeout=60, env=env)
