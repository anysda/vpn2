"""Договор ./deploy.sh с оператором до изменений на узлах и общие помощники тестов деплоя.

VPN2_TEST_ROOT - другая ревизия под теми же проверками.
"""
import os
import shlex
import shutil
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


def source(path):
    return (ROOT / path).read_text()


def function(name):
    """Функция bash по имени из оркестратора деплоя или его библиотек."""
    for path in [ROOT / 'infra/deploy.sh', *sorted((ROOT / 'infra/lib').glob('*.sh'))]:
        text = path.read_text()
        if f'\n{name}() {{' in text:
            return f'{name}() {{' + text.split(f'\n{name}() {{', 1)[1].split('\n}', 1)[0] + '\n}\n'
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


class DeployContractTests(unittest.TestCase):
    def test_help_needs_no_config(self):
        for flag in ('--help', '-h'):
            for config in (None, 'invalid config: ['):
                with self.subTest(flag=flag, config=config), tempfile.TemporaryDirectory() as directory:
                    result = deploy(directory, flag, config=config)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('./deploy.sh <stage> <group>', result.stdout)

    def test_missing_or_invalid_config_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            result = deploy(directory, config=None)
            self.assertEqual(result.returncode, 1)
            self.assertIn('config.yaml не найден', result.stdout)
            self.assertIn('./setup.sh', result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            result = deploy(directory, '30-frontend', 'ru', config="entry:\n  password: 'CANARY\n")
            self.assertEqual(result.returncode, 1)
            self.assertIn('ERROR: config.yaml:', result.stderr)
            self.assertNotIn('CANARY', result.stdout + result.stderr)

    def test_second_deploy_on_the_machine_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'deploy.lock'
            with open(lock, 'w') as handle, subprocess.Popen(['flock', '-n', str(lock), 'sleep', '30']) as holder:
                del handle
                try:
                    for _ in range(50):
                        if subprocess.run(['flock', '-n', str(lock), 'true']).returncode:
                            break
                    result = deploy(directory, '30-frontend', 'ru')
                finally:
                    holder.kill()
            self.assertEqual(result.returncode, 1)
            self.assertIn('deploy уже выполняется на этой машине', result.stderr)

    def test_wrong_arguments_are_refused_before_any_change(self):
        cases = ((('30-frontend',), 'использование: ./deploy.sh <stage> <group>'),
                 (('30-frontnd', 'ru'), 'нет такой стадии: 30-frontnd'),
                 (('30-frontend', 'nosuch'), 'unknown host group: nosuch'),
                 (('30-frontend', 'us'), '30-frontend - стадия entry, на экзитах её нет'),
                 (('30-frontend',), 'использование'))
        for index, (args, text) in enumerate(cases):
            without = ('HOME',) if index == len(cases) - 1 else ()
            with self.subTest(args=args, without=without), tempfile.TemporaryDirectory() as directory:
                result = deploy(directory, *args, without=without)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(text, result.stderr)
                self.assertNotIn('unbound variable', result.stderr)
                self.assertNotIn('проверяю локальные зависимости', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
