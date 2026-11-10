"""VPN2-637: блок failover: убрали из config.yaml и сделали deploy.

RESTART_AFTER_CHECKS и RESTART_MIN_INTERVAL_S живут только в config.yaml:
есть ключ - его значение, нет - умолчание 3/600. Прошлое значение из
/etc/anysda/failover-watchdog.env для них не читается, иначе однажды
выставленный 0 выключает перезапуск навсегда.

Два прогона команды с одним env-файлом: файл от первого
прогона остаётся ко второму, как на entry между двумя deploy.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import test_config_validation as validation
import watchdog_src


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
OFF = 'failover:\n  restart_after_checks: 0\n  restart_min_interval: 900\n'


class Box:
    """Env-файл сторожа между прогонами vpn2 watchdog failover-env."""

    def __init__(self, root):
        self.root = root
        self.runs = 0
        self.secret = root / 'clash-secret.txt'
        self.secret.write_text('test\n')

    @property
    def env_file(self):
        return self.root / 'failover-watchdog.env'

    def deploy(self, failover):
        """config.yaml -> config2env -> окружение стадии -> env сторожа."""
        self.runs += 1
        cfg = self.root / f'cfg{self.runs}'
        cfg.mkdir()
        result = validation.ConfigValidationTests.convert(None, validation.CONFIG + failover, str(cfg))
        if result.returncode:
            return f'config2env: {result.stdout}{result.stderr}'
        envs = cfg / 'infra/envs'
        rendered = cfg / 'ru.env'
        rendered.write_text((envs / 'all.env').read_text() + '\n' + ''.join(
            line + '\n' for line in (envs / 'ru.env').read_text().splitlines()
            if not line.startswith('SSH_PASS=')))
        result = subprocess.run(
            ['bash', '-c', 'set -a; source "$1"; shift; exec "$@"', 'bash', str(rendered),
             *watchdog_src.command('failover-env'), '--env-file', str(self.env_file),
             '--secret-file', str(self.secret)],
            capture_output=True, text=True, timeout=30)
        if result.returncode:
            return f'failover-env: {result.stdout}{result.stderr}'
        return dict(line.partition('=')[::2] for line in self.env_file.read_text().splitlines())


def restart_keys(env):
    return env.get('RESTART_AFTER_CHECKS'), env.get('RESTART_MIN_INTERVAL_S')


class TestFailoverBlockRemoved(unittest.TestCase):

    def test_block_removed_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = Box(Path(tmp))
            env = box.deploy(OFF)
            self.assertIsInstance(env, dict, env)
            self.assertEqual(restart_keys(env), ('0', '900'), 'прогон 1: блок 0/900')
            env = box.deploy('')
            self.assertIsInstance(env, dict, env)
            self.assertEqual(restart_keys(env), ('3', '600'),
                             'блок failover удалён - умолчания 3/600, а не прошлые 0/900')

    def test_block_changed_takes_new_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = Box(Path(tmp))
            env = box.deploy(OFF)
            self.assertIsInstance(env, dict, env)
            env = box.deploy('failover:\n  restart_after_checks: 5\n  restart_min_interval: 700\n')
            self.assertIsInstance(env, dict, env)
            self.assertEqual(restart_keys(env), ('5', '700'), 'прогон 2 с блоком 5/700')

    def test_manual_edit_of_other_keys_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = Box(Path(tmp))
            env = box.deploy(OFF)
            self.assertIsInstance(env, dict, env)
            box.env_file.write_text(box.env_file.read_text().replace(
                f"MIN_DWELL_S={env['MIN_DWELL_S']}", 'MIN_DWELL_S=1234'))
            env = box.deploy('')
            self.assertIsInstance(env, dict, env)
            self.assertEqual(env.get('MIN_DWELL_S'), '1234', 'ручная правка прочих ключей сохраняется')


if __name__ == '__main__':
    unittest.main()
