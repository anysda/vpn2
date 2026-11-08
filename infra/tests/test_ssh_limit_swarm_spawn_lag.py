"""Рой порождается медленнее замера: предел истекает на всём рое, а не на его части.

Предел test_11 считается от замера порождения роя до прогона. Под нагрузкой
сам прогон порождает рой медленнее замера, и к пределу в сессии меньше
половины детей. Здесь замер быстрый, а порождение в прогоне замедлено: по
0,01 с на ребёнка.
"""
import unittest
from unittest import mock

import test_ssh_limit_session as session

LOOP = 'for ((i = 0; i < FAKE_MANY / 2; i++)); do '


class SwarmSpawnLagTests(unittest.TestCase):
    assert_nothing_left = session.SshLimitSessionTests.assert_nothing_left

    def test_limit_counts_from_spawned_swarm(self):
        self.assertEqual(session.FAKE_SSH.count(LOOP), 1, 'цикл роя в заглушке не найден')
        slow = session.FAKE_SSH.replace(LOOP, LOOP + 'sleep 0.01; ')
        with mock.patch.object(session, 'FAKE_SSH', slow), \
                mock.patch.object(session, 'swarm_spawn_seconds', return_value=0.4):
            session.SshLimitSessionTests.test_11_swarm_returns_in_time(self)


if __name__ == '__main__':
    unittest.main()
