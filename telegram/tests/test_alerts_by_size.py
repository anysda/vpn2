"""Пороги CPU/RAM и окно зависят от размера узла (cores, memTotalBytes в статусе
панели) и перекрываются явным env. Узел без размера - пороги как раньше."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from support import load_bot

GIB = 1024 ** 3
SMALL = {'cores': 1, 'memTotalBytes': 2 * GIB}
BIG = {'cores': 4, 'memTotalBytes': 8 * GIB}


def node(tag, cpu=10, ram=10, size=None):
    return {'tag': tag, 'staleSec': 0, 'cpu': cpu, 'ram': ram, **(size or {})}


class AlertsBySizeTests(unittest.IsolatedAsyncioTestCase):
    async def run_samples(self, samples, env=None):
        """Гонит monitor_loop по снимкам; возвращает [(номер сэмпла с 1, текст)]."""
        bot = load_bot(env)
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        sample = 0
        sent = []

        async def status():
            nonlocal sample
            sample += 1
            return {'nodes': samples[sample - 1]}

        async def send(_chat, text, *_args, **_kwargs):
            sent.append((sample, text))
            return {'ok': True}

        with patch.object(bot, 'api_status', AsyncMock(side_effect=status)), \
                patch.object(bot.asyncio, 'sleep', AsyncMock(side_effect=[
                    None] * len(samples) + [asyncio.CancelledError()])), \
                patch.object(bot, 'tg_send', AsyncMock(side_effect=send)):
            with self.assertRaises(asyncio.CancelledError):
                await bot.monitor_loop()
        return sent

    @staticmethod
    def alerts(sent, tag, label):
        return [i for i, text in sent
                if tag.upper() in text and label in text and 'норм' not in text]

    @staticmethod
    def recoveries(sent, tag, label):
        return [i for i, text in sent if tag.upper() in text and label in text and 'норм' in text]

    async def test_small_node_cpu_90_for_three_minutes_is_quiet(self):
        sent = await self.run_samples([[node('ru', cpu=90, size=SMALL)]] * 6)
        self.assertEqual(self.alerts(sent, 'ru', 'CPU'), [], sent)

    async def test_small_node_cpu_97_alerts_once_after_five_minutes_then_recovers(self):
        samples = [[node('ru', cpu=97, size=SMALL)]] * 12 + [[node('ru', cpu=20, size=SMALL)]]
        sent = await self.run_samples(samples)
        # Окно 5 мин = 10 сэмплов по 30 с: тревога ровно на 10-м, не раньше.
        self.assertEqual(self.alerts(sent, 'ru', 'CPU'), [10], sent)
        self.assertEqual(self.recoveries(sent, 'ru', 'CPU'), [13], sent)

    async def test_small_node_ram_85_is_quiet_ram_92_alerts_once(self):
        sent = await self.run_samples([[node('ru', ram=85, size=SMALL)]] * 20)
        self.assertEqual(self.alerts(sent, 'ru', 'RAM'), [], sent)
        sent = await self.run_samples([[node('ru', ram=92, size=SMALL)]] * 12)
        self.assertEqual(len(self.alerts(sent, 'ru', 'RAM')), 1, sent)

    async def test_big_node_cpu_85_alerts_after_two_samples(self):
        sent = await self.run_samples([[node('se', cpu=85, size=BIG)]] * 2)
        self.assertEqual(self.alerts(sent, 'se', 'CPU'), [2], sent)

    async def test_two_nodes_keep_own_thresholds_and_counters(self):
        samples = [[node('ru', cpu=97, size=SMALL), node('se', cpu=85 if i < 2 else 20, size=BIG)]
                   for i in range(12)]
        sent = await self.run_samples(samples)
        self.assertEqual(self.alerts(sent, 'se', 'CPU'), [2], sent)
        self.assertEqual(self.recoveries(sent, 'se', 'CPU'), [3], sent)
        self.assertEqual(self.alerts(sent, 'ru', 'CPU'), [10], sent)
        self.assertEqual(self.recoveries(sent, 'ru', 'CPU'), [], sent)

    async def test_node_without_size_keeps_old_thresholds(self):
        sent = await self.run_samples([[node('ru', cpu=85, ram=85)]] * 2)
        self.assertEqual(self.alerts(sent, 'ru', 'CPU'), [2], sent)
        self.assertEqual(self.alerts(sent, 'ru', 'RAM'), [2], sent)

    async def test_explicit_env_overrides_size_calculation(self):
        # Окно 120 с = 4 сэмпла: тревога на 4-м, порог 70 вместо расчётных 95/90.
        env = {'ALERT_CPU_PCT': '70', 'ALERT_RAM_PCT': '70', 'ALERT_WINDOW_SEC': '120'}
        sent = await self.run_samples([[node('ru', cpu=75, ram=75, size=SMALL)]] * 6, env)
        self.assertEqual(self.alerts(sent, 'ru', 'CPU'), [4], sent)
        self.assertEqual(self.alerts(sent, 'ru', 'RAM'), [4], sent)


if __name__ == '__main__':
    unittest.main()
