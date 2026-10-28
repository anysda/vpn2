"""VPN2-565: the admin presses the start link just as the entry comes back.

The exits poll the entry in a round, the admin opens the link, and the
entry's replies arrive before the next round applies the press. No emergency
may start, and the admin's message must still be corrected with the link
closed, as when the entry returns before any press.
"""

import unittest

import test_emergency_manual_start as manual


class PressAsEntryReturnsTests(manual.ManualStartTests):
    def test_press_applied_after_entry_returned_corrects_message(self):
        cluster = self.exits()
        self.silent(cluster)
        call, url = self.outage_message()
        cluster.start('entry')
        cluster.drain()
        # One round by hand: the exits poll the entry, the admin presses, and
        # only then do the entry's replies arrive.
        cluster.clock.now += 15
        for agent in cluster.agents.values():
            agent.tick(cluster.clock())
        status = self.open(cluster, url)
        self.assertTrue(status is None or status in (202, 410), f'HTTP {status}')
        cluster.drain()
        cluster.advance(120)
        self.assertEqual(cluster.active(), [])
        self.assertEqual(cluster.leaders(), [])
        self.assertEqual(cluster.starts(), 0)
        self.assertIn(manual.BACK, self.bot.text(call), 'сообщение админу не исправлено')
        servers = {node: agent.lifecycle.server for node, agent in cluster.exit_agents().items()}
        self.assertEqual({node: server for node, server in servers.items() if server}, {},
                         'сервер на 443 слушает без аварии')
        self.assertIsNone(self.open(cluster, url), 'ссылка ещё отвечает')
        for agent in cluster.exit_agents().values():
            self.assertIsNone(agent.state.data.get('outage_notice'))


# Only this edge: the inherited cases run in test_emergency_manual_start.
for _name in [name for name in dir(manual.ManualStartTests) if name.startswith('test_')]:
    setattr(PressAsEntryReturnsTests, _name, None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
