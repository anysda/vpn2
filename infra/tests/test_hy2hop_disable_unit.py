"""Выключение смены портов hysteria2 на экзите останавливает юнит anysda-hy2hop (VPN2-642).

Стадия 10-foreign при `ports.hy2_hop.enabled: false` удаляла файл юнита, не
остановив его: до перезагрузки systemd показывал `active (exited)`.
"""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import test_hy2_port_hopping as hop


UNIT = 'etc/systemd/system/anysda-hy2hop.service'
LEFTOVERS = (UNIT, 'etc/systemd/system/nftables.service.d/90-anysda-hy2hop.conf',
             'etc/nftables-hy2hop.conf')
STOP = re.compile(r'systemctl (?=.*\banysda-hy2hop\b)'
                  r'(?=.*\bstop\b|.*\bdisable\b.*--now|.*--now\b.*\bdisable\b)')


class Hy2HopDisableUnitTests(unittest.TestCase):
    prepare_stage = hop.Hy2PortHoppingTests.prepare_stage
    rewrite = staticmethod(hop.Hy2PortHoppingTests.rewrite)
    stage_env = staticmethod(hop.Hy2PortHoppingTests.stage_env)
    texts = staticmethod(hop.Hy2PortHoppingTests.texts)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.envs = {}
        for enabled in ('true', 'false'):
            directory = self.root / f'config-{enabled}'
            directory.mkdir()
            result = hop.convert(hop.with_hop(enabled), str(directory))
            self.assertEqual(result.returncode, 0, result.stderr)
            envs = directory / 'infra/envs'
            env_file = self.root / f'stage-{enabled}.env'
            env_file.write_text((envs / 'all.env').read_text() + (envs / 'gb.env').read_text())
            self.envs[enabled] = env_file
        self.listen = hop.load_env(str(self.root / 'config-true'), 'gb')
        self.stage, self.logs = self.prepare_stage(self.root)

    def run_stage(self, enabled):
        """Прогон стадии; журнал вызовов - только этого прогона."""
        self.logs['calls'].write_text('')
        self.logs['nft'].write_text('')
        result = subprocess.run(['bash', str(self.stage), str(self.envs[enabled])], cwd=self.root,
                                env=self.stage_env(self.root, self.logs), capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         f'enabled={enabled}\n{result.stdout}\n{result.stderr}')
        return self.logs['calls'].read_text().splitlines()

    def assert_no_leftovers(self):
        left = [name for name in LEFTOVERS if (self.root / name).exists()]
        self.assertEqual(left, [], 'после выключения остались файлы смены портов')

    # край 1
    def test_disable_after_enable_stops_unit_before_daemon_reload(self):
        calls = self.run_stage('true')
        self.assertTrue((self.root / UNIT).is_file(), 'включённая смена портов не создала юнит')
        self.assertIn('systemctl enable anysda-hy2hop.service', calls)

        calls = self.run_stage('false')
        self.assert_no_leftovers()
        reloads = [i for i, line in enumerate(calls) if line == 'systemctl daemon-reload']
        self.assertTrue(reloads, f'нет daemon-reload после удаления юнита: {calls}')
        stops = [i for i, line in enumerate(calls) if STOP.match(line)]
        self.assertTrue(stops and stops[0] < reloads[-1],
                        'юнит anysda-hy2hop удалён без остановки (systemctl stop или '
                        f'disable --now до daemon-reload): {calls}')

    # край 2
    def test_disabled_from_start_is_green(self):
        self.run_stage('false')
        self.assert_no_leftovers()

    # край 3
    def test_disabled_rerun_twice_is_green(self):
        for _ in range(2):
            self.run_stage('false')
            self.assert_no_leftovers()

    # край 4
    def test_enable_after_disable_restores_unit_and_redirects(self):
        self.run_stage('false')
        calls = self.run_stage('true')
        self.assertTrue((self.root / UNIT).is_file(), 'юнит не вернулся после включения')
        self.assertIn('systemctl enable anysda-hy2hop.service', calls)
        files = dict(self.texts(self.root / 'etc', self.root / 'bin'))
        listen = {'direct': self.listen['HY2_DIRECT_PORT'], 'warp': self.listen['HY2_WARP_PORT'],
                  'mgmt': self.listen['HY2_MGMT_PORT']}
        for channel in hop.CHANNELS:
            with self.subTest(channel=channel):
                low, high = hop.DEFAULTS[channel]
                rule = rf'udp dport {low}-{high}\b[^\n;]*?\bredirect to :?{listen[channel]}\b'
                self.assertEqual(sum(len(re.findall(rule, text)) for text in files.values()), 1,
                                 sorted(files))


if __name__ == '__main__':
    unittest.main()
