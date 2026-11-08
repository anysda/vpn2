"""Проверка внешней точки переживает переключение транспорта до экзита.

На стенде hy2-транспорт entry -> экзит пропал на ~25 с, сторож перевёл его на
tcp, а одиночная health-проба ретранслятора попала в провал: все внешние пробы
портов ушли в «не проверено». Часы виртуальные: неудачная health стоит свой
предел (5 с connect + 8 с ответ), пауза - сколько просит повтор.
"""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'infra/lib/verify-traffic.py'
HEALTH_LIMIT = 13.0
WINDOW_LIMIT = 90.0


def load():
    spec = importlib.util.spec_from_file_location('verify_traffic', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vt = load()


class Clock:
    def __init__(self, outage):
        self.now = 0.0
        self.outage = outage
        self.calls = 0

    def sleep(self, seconds):
        self.now += seconds

    def health(self, _port):
        self.calls += 1
        if self.now < self.outage:
            self.now += HEALTH_LIMIT
            return False
        self.now += 0.1
        return True


def reachable(outage):
    clock = Clock(outage)
    with mock.patch.object(vt.time, 'sleep', clock.sleep):
        result = vt.relay_reachable(clock, 18080)
    return result, clock


class RelayHealthWindowTest(unittest.TestCase):
    def test_transport_switch_outage_is_survived(self):
        result, clock = reachable(30.0)
        self.assertTrue(result, f'провал 30 с, ответ на {clock.now:.0f} с - внешняя точка есть')

    def test_live_path_answers_at_once(self):
        result, clock = reachable(0.0)
        self.assertTrue(result)
        self.assertEqual(clock.calls, 1)
        self.assertLess(clock.now, 1.0)

    def test_dead_path_is_bounded(self):
        result, clock = reachable(float('inf'))
        self.assertFalse(result)
        self.assertLessEqual(clock.now, WINDOW_LIMIT, 'мёртвый путь не держит проверку дольше окна')


if __name__ == '__main__':
    unittest.main()
