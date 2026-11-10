"""Сторожа пакета vpn2 ведут себя как файлы infra/lib.

Сценарии сторожей идут второй раз, по vpn2.watchdog (watchdog_src,
VPN2_WATCHDOG_IMPL=package): расхождение пакета со старым файлом роняет тот же
тест, что стережёт старый файл.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import unittest

import watchdog_src

HERE = Path(__file__).resolve().parent
# Наборы, что грузят сторожа сами или через загрузчик соседнего набора.
SUITES = (
    'test_failover_audit', 'test_failover_entry_pressure', 'test_failover_hy2_rise',
    'test_failover_node_channel_alive', 'test_failover_restart', 'test_failover_stuck_exit',
    'test_route_pin_stuck_exit', 'test_tcp_fallback', 'test_tcp_node_events',
    'test_tcp_return_window', 'test_transport', 'test_warp_bootstrap_restart',
    'test_warp_dead_node_penalty', 'test_warp_fallback_dead_after', 'test_warp_pin_egress',
    'test_warp_watchdog', 'test_watchdog_opener_memory', 'test_yt_bypass_failures',
    'test_yt_failover', 'test_yt_pot', 'test_yt_probe_limits', 'test_yt_probe_timeout',
    'test_yt_unmeasured', 'test_yt_unmeasured_alerts',
)
# Меряет время по настоящим часам: идёт одна, без соседей по процессору.
TIMED = ('test_udp_cut_switch_time',)


def run(suite):
    result = subprocess.run([sys.executable, '-m', 'unittest', suite], cwd=HERE,
                            env=dict(os.environ, VPN2_WATCHDOG_IMPL='package'),
                            capture_output=True, text=True, timeout=900)
    return suite, result.returncode, result.stderr[-3000:]


@unittest.skipIf(watchdog_src.PACKAGE, 'набор уже идёт по пакету')
class WatchdogPackageTests(unittest.TestCase):
    def test_every_loader_of_a_watchdog_is_listed(self):
        loaders = {path.stem for path in HERE.glob('test_*.py')
                   if 'watchdog_src.' in path.read_text() and path.stem != Path(__file__).stem}
        self.assertEqual(loaders - set(SUITES + TIMED), set())

    def test_package_passes_the_watchdog_suites(self):
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(run, SUITES))
        results += [run(suite) for suite in TIMED]
        failed = [f'{suite}:\n{tail}' for suite, code, tail in results if code]
        self.assertEqual(failed, [], '\n'.join(failed))
        ran = [tail.rsplit('Ran ', 1)[-1].split()[0] for _, _, tail in results]
        self.assertTrue(all(count.isdigit() and int(count) for count in ran), ran)


if __name__ == '__main__':
    unittest.main()
