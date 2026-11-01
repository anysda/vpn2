"""Включение смены портов hysteria2 на экзите запускает юнит anysda-hy2hop (VPN2-643).

Стадия 10-foreign при `ports.hy2_hop.enabled: true` грузила таблицу прямым
`nft -f` и только включала юнит: до перезагрузки systemd показывал
`inactive (dead)`, и смена портов выглядела выключенной.
"""
import re
import unittest

import test_hy2_port_hopping as hop
import test_hy2hop_disable_unit as base


START = re.compile(r'systemctl (?=.*\banysda-hy2hop\b)'
                   r'(?=.*\b(?:start|restart)\b|.*\benable\b.*--now|.*--now\b.*\benable\b)')


class Hy2HopEnableStartsUnitTests(unittest.TestCase):
    prepare_stage = base.Hy2HopDisableUnitTests.prepare_stage
    rewrite = staticmethod(base.Hy2HopDisableUnitTests.rewrite)
    stage_env = staticmethod(base.Hy2HopDisableUnitTests.stage_env)
    texts = staticmethod(base.Hy2HopDisableUnitTests.texts)
    setUp = base.Hy2HopDisableUnitTests.setUp
    run_stage = base.Hy2HopDisableUnitTests.run_stage

    def assert_started_after_reload(self, calls):
        self.assertTrue((self.root / base.UNIT).is_file(), 'включённая смена портов не создала юнит')
        reloads = [i for i, line in enumerate(calls) if line == 'systemctl daemon-reload']
        self.assertTrue(reloads, f'нет daemon-reload после записи юнита: {calls}')
        starts = [i for i, line in enumerate(calls) if START.match(line)]
        self.assertTrue(starts and starts[-1] > reloads[-1],
                        'юнит anysda-hy2hop включён без запуска (systemctl start|restart или '
                        f'enable --now после daemon-reload): {calls}')

    def assert_single_redirects(self):
        files = dict(self.texts(self.root / 'etc', self.root / 'bin'))
        listen = {'direct': self.listen['HY2_DIRECT_PORT'], 'warp': self.listen['HY2_WARP_PORT'],
                  'mgmt': self.listen['HY2_MGMT_PORT']}
        for channel in hop.CHANNELS:
            with self.subTest(channel=channel):
                low, high = hop.DEFAULTS[channel]
                rule = rf'udp dport {low}-{high}\b[^\n;]*?\bredirect to :?{listen[channel]}\b'
                self.assertEqual(sum(len(re.findall(rule, text)) for text in files.values()), 1,
                                 sorted(files))

    # край 1
    def test_first_install_enabled_starts_unit(self):
        self.assertFalse((self.root / base.UNIT).exists())
        self.assert_started_after_reload(self.run_stage('true'))

    # край 2
    def test_enable_after_disable_starts_unit(self):
        self.run_stage('false')
        self.assert_started_after_reload(self.run_stage('true'))

    # край 3
    def test_enabled_rerun_twice_starts_unit_each_time(self):
        for run in range(2):
            with self.subTest(run=run):
                self.assert_started_after_reload(self.run_stage('true'))
                self.assert_single_redirects()

    # край 4
    def test_disabled_does_not_start_unit(self):
        calls = self.run_stage('false')
        self.assertEqual([line for line in calls if START.match(line)], [],
                         'выключенная смена портов запускает юнит anysda-hy2hop')


if __name__ == '__main__':
    unittest.main()
