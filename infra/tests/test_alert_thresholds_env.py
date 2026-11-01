"""Пороги тревог из config.yaml (alerts.cpu_pct, alerts.ram_pct, alerts.window)
доходят до контейнера бота: config2env -> envs/all.env + envs/ru.env -> стадия 35.
Нет раздела alerts - нет и переменных, бот считает пороги сам."""
import subprocess
import tempfile
import unittest

import test_config_validation as validation
import test_telegram_stage as stage

ALERTS = 'alerts:\n  cpu_pct: 70\n  ram_pct: 85\n  window: 300\n'
EXPECTED = {'ALERT_CPU_PCT': '70', 'ALERT_RAM_PCT': '85', 'ALERT_WINDOW_SEC': '300'}


class AlertThresholdsEnvTests(unittest.TestCase):
    setUp = stage.TelegramStageTests.setUp
    run_stage = stage.TelegramStageTests.run_stage
    ok = stage.TelegramStageTests.ok
    data = stage.TelegramStageTests.data
    container_env = stage.TelegramStageTests.container_env

    def stage_alerts(self, config):
        """ALERT_* из склейки all.env + ru.env, как её получает стадия на entry."""
        with tempfile.TemporaryDirectory() as directory:
            result = validation.ConfigValidationTests.convert(self, config, directory)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            dump = subprocess.run(
                ['bash', '-c', 'set -a; source infra/envs/all.env; source infra/envs/ru.env; env -0'],
                cwd=directory, capture_output=True, timeout=10, env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(dump.returncode, 0, dump.stderr)
        pairs = (item.split('=', 1) for item in dump.stdout.decode().split('\0') if '=' in item)
        return {k: v for k, v in pairs if k.startswith('ALERT_')}

    def bot_alerts(self, env):
        self.ok(**env)
        return {k: v for k, v in self.container_env('anysda-tgbot').items()
                if k.startswith('ALERT_')}

    def test_alerts_section_reaches_bot_container(self):
        env = self.stage_alerts(validation.CONFIG + ALERTS)
        self.assertEqual(env, EXPECTED)
        self.assertEqual(self.bot_alerts(env), EXPECTED)

    def test_no_alerts_section_means_no_variables(self):
        env = self.stage_alerts(validation.CONFIG)
        self.assertEqual(env, {})
        self.assertEqual(self.bot_alerts(env), {})


if __name__ == '__main__':
    unittest.main()
