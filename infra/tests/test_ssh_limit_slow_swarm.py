"""Рой из 2000 детей на медленном раннере: предел истекает на рое, а не после него.

test_11 берёт предел вдвое дольше порождения роя. Под нагрузкой рой
порождается 15 с и дольше, и предел доходит до 30 с и выше. Дети роя
спят не меньше предела, иначе команда кончается сама до предела и rc=0.
Медленный раннер здесь - замер порождения, подменённый на 16 с.
"""
import unittest
from unittest import mock

import test_ssh_limit_session as session


class SlowSwarmTests(unittest.TestCase):
    assert_nothing_left = session.SshLimitSessionTests.assert_nothing_left

    def test_swarm_outlives_limit_on_slow_runner(self):
        with mock.patch.object(session, 'swarm_spawn_seconds', return_value=16.0):
            session.SshLimitSessionTests.test_11_swarm_returns_in_time(self)


if __name__ == '__main__':
    unittest.main()
