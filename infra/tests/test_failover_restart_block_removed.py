"""VPN2-637: блок failover: убрали из config.yaml и сделали deploy.

RESTART_AFTER_CHECKS и RESTART_MIN_INTERVAL_S живут только в config.yaml:
есть ключ - его значение, нет - умолчание 3/600. Прошлое значение из
/etc/anysda/failover-watchdog.env для них не читается, иначе однажды
выставленный 0 выключает перезапуск навсегда.

Два прогона стадии 21 в одной песочнице: env-файл сторожа от первого
прогона остаётся ко второму, как на entry между двумя deploy.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import test_config_validation as validation


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
OFF = 'failover:\n  restart_after_checks: 0\n  restart_min_interval: 900\n'


class Box:
    """Песочница стадии 21: /etc, /var, /usr/local под одним каталогом."""

    def __init__(self, root):
        self.root = root
        self.box = root / 'box'
        self.runs = 0
        for path in ('etc/anysda', 'etc/systemd/system', 'tmp/anysda', 'usr/local/bin', 'usr/local/sbin'):
            (self.box / path).mkdir(parents=True)
        (self.box / 'etc/anysda/clash-secret.txt').write_text('test\n')
        (self.box / 'tmp/anysda/failover-watchdog.py').write_text('')
        self.bin = root / 'bin'
        self.bin.mkdir()
        stubs = {'systemctl': ':', 'sleep': ':', 'install': 'cp "${@: -2:1}" "${@: -1}"'}
        for name, body in stubs.items():
            (self.bin / name).write_text(f'#!/bin/bash\n{body}\n')
            (self.bin / name).chmod(0o755)
        stage = root / 'stage'
        stage.mkdir()
        text = (ROOT / 'infra/scripts/21-failover-watchdog.sh').read_text()
        for prefix in ('/etc/', '/var/', '/usr/local/', '/tmp/anysda'):
            text = text.replace(prefix, f'{self.box}{prefix}')
        self.stage = stage / '21-failover-watchdog.sh'
        self.stage.write_text(text)
        (stage / 'anysda-svc.sh').write_text((ROOT / 'infra/lib/anysda-svc.sh').read_text())

    @property
    def env_file(self):
        return self.box / 'etc/anysda/failover-watchdog.env'

    def deploy(self, failover):
        """config.yaml -> config2env -> env стадии -> стадия 21 -> env сторожа."""
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
            ['bash', str(self.stage), str(rendered)],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}',
                 'SVC_FP_DIR': str(self.box / 'var/lib/anysda/applied')})
        if result.returncode:
            return f'стадия 21: {result.stdout}{result.stderr}'
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
