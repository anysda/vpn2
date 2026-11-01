"""Размер узла из статуса панели может на сэмпл пропасть (запрос к VM не успел).
Тогда пороги берутся по последнему известному размеру, а не откатываются на 80%."""
import logging
import unittest

import test_alerts_by_size as base

SMALL, node = base.SMALL, base.node
GAP = {'cores': None, 'memTotalBytes': None}


class AlertsSizeGapTests(unittest.IsolatedAsyncioTestCase):
    run_samples = base.AlertsBySizeTests.run_samples
    alerts = staticmethod(base.AlertsBySizeTests.alerts)

    async def test_small_node_size_gap_keeps_small_thresholds(self):
        samples = [[node('ru', cpu=90, size=SMALL)]] * 5 + [[node('ru', cpu=90, size=GAP)]] * 2
        with self.assertLogs(level=logging.INFO) as logs:
            sent = await self.run_samples(samples)
        self.assertEqual(self.alerts(sent, 'ru', 'CPU'), [], sent)
        lines = [line for line in logs.output if 'пороги узла' in line]
        self.assertEqual(len(lines), 1, lines)

    async def test_node_never_sized_keeps_old_thresholds(self):
        sent = await self.run_samples([[node('ru', cpu=85, ram=85, size=GAP)]] * 2)
        self.assertEqual(self.alerts(sent, 'ru', 'CPU'), [2], sent)
        self.assertEqual(self.alerts(sent, 'ru', 'RAM'), [2], sent)


if __name__ == '__main__':
    unittest.main()
