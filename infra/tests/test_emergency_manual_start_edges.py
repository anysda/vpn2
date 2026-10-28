"""VPN2-565: the admin's notice never lies about the entry or the emergency.

Edges of the manual start: the sending exit reboots while the entry is still
dead, a correction that Telegram refused meets an automatic start, the admin
presses while the entry is already heard again. Plus regressions: the agent
restarts between the message and the press, the first message fails once.
Same harness as test_emergency_manual_start: real agents over UDP, a fake
Bot API, HTTPS to the exit the button names.
"""

import http.client
import json
import ssl
import unittest
from urllib.parse import urlsplit, urlunsplit

import test_emergency_manual_start as manual

NOT_STARTED = 'не включался'
NOT_NEEDED = 'Entry снова доступен'


class FlakyBotHandler(manual.BotHandler):
    """sendMessage can fail like a Bot API that is briefly unreachable."""

    def do_POST(self):  # noqa - HTTP handler
        bot = self.server
        if not (self.path.endswith('/sendMessage') and getattr(bot, 'fail_sends', False)):
            return super().do_POST()
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])) or b'{}')
        with bot.lock:
            bot.calls.append(dict(method='sendMessage', data=data, ok=False, message_id=None))
        return self.reply(502, {'ok': False, 'error_code': 502, 'description': 'Bad Gateway'})


class ManualStartEdgeTests(manual.ManualStartTests):
    def setUp(self):
        super().setUp()
        setattr(self.bot, 'RequestHandlerClass', FlakyBotHandler)

    def restart(self, cluster, node, reboot):
        """A new agent process on the same state; a reboot also changes the boot id."""
        agent = cluster.agents[node]
        agent.lifecycle.close()
        cluster.stop(node)
        if reboot:
            path = cluster.directory / node / 'agent.json'
            data = json.loads(path.read_text())
            data['clock_boot'] = 'rebooted-' + data['clock_boot']
            path.write_text(json.dumps(data))
        cluster.start(node)
        cluster.drain()

    def press(self, cluster, url):
        """Open the link: (HTTP status, page text) or (None, '') when nothing listens."""
        parts = urlsplit(url)
        node = next(node for node, agent in cluster.exit_agents().items()
                    if agent.addresses[node][0] == parts.hostname)
        server = cluster.agents[node].lifecycle.server
        if server is None:
            return None, ''
        connection = http.client.HTTPSConnection(
            parts.hostname, server.server_address[1], timeout=5,
            context=ssl._create_unverified_context())  # noqa: S323 - local self-signed panel
        try:
            connection.request('GET', urlunsplit(('', '', parts.path, parts.query, '')))
            response = connection.getresponse()
            return response.status, response.read().decode()
        except ConnectionError:
            return None, ''
        finally:
            connection.close()

    def assert_started_once(self, cluster):
        for _ in range(8):
            cluster.advance(15)
            self.assertLessEqual(len(cluster.leaders()), 1, cluster.leaders())
        self.assertEqual(len(cluster.leaders()), 1, 'по нажатию аварии нет')
        self.assertEqual(cluster.active(), ['a', 'b', 'c'])
        self.assertEqual(cluster.starts(), 1)

    # Edge 1. The link stays valid: the message is unchanged and still true,
    # its expiry is wall time, and only the entry itself can prove it is back.
    def test_sender_reboot_with_dead_entry_keeps_message_and_link(self):
        cluster = self.exits()
        self.silent(cluster)
        call, url = self.outage_message()
        self.restart(cluster, 'a', reboot=True)
        for _ in range(8):
            cluster.advance(15)
            self.assertNotIn(manual.BACK, self.bot.text(call), 'entry мёртв, а сообщение «снова доступен»')
        self.outage_message()
        self.assertEqual(len(self.bot.sent(manual.ADMIN)), 1, self.admin_texts())
        status, page = self.press(cluster, url)
        self.assertEqual(status, 202, f'после перезагрузки экзита ссылка не работает: {status} {page}')
        self.assert_started_once(cluster)

    # Edge 2.
    def test_refused_correction_never_arrives_during_automatic_emergency(self):
        cluster = self.exits()
        self.silent(cluster)
        call, _url = self.outage_message()
        self.bot.fail_edits = True
        cluster.start('entry')
        cluster.drain()
        cluster.advance(30)
        self.assertTrue(self.bot.edits(call['message_id'], ok=False), 'исправление не пытались отправить')
        cluster.stop('entry')
        cluster.advance(1320)
        self.assertEqual(len(cluster.leaders()), 1, 'автостарт не случился')
        self.bot.fail_edits = False
        cluster.advance(120)
        self.assertNotEqual(cluster.active(), [], 'авария погасла')
        text = self.bot.text(call)
        self.assertNotIn(NOT_STARTED, text, f'при идущей аварии админу: «{text}»')
        self.assertNotIn(manual.BACK, text, f'entry мёртв, а админу: «{text}»')

    # Edge 3.
    def test_press_while_entry_heard_answers_410_and_starts_nothing(self):
        cluster = self.exits()
        self.silent(cluster)
        call, url = self.outage_message()
        cluster.start('entry')
        cluster.drain()
        # One round by hand: the exits poll, the entry answers, and the admin
        # presses before the next round looks at the notice.
        cluster.clock.now += 15
        for agent in cluster.agents.values():
            agent.tick(cluster.clock())
        cluster.drain()
        status, page = self.press(cluster, url)
        self.assertEqual(status, 410, f'нажатие при слышимом entry: {status} {page}')
        self.assertIn(NOT_NEEDED, page)
        self.assertNotIn('запускается', page)
        cluster.advance(120)
        self.assertEqual(cluster.active(), [])
        self.assertEqual(cluster.leaders(), [])
        self.assertEqual(cluster.starts(), 0)
        self.assertIn(manual.BACK, self.bot.text(call))

    # Regressions.
    def test_agent_restart_between_message_and_press(self):
        cluster = self.exits()
        self.silent(cluster)
        call, url = self.outage_message()
        self.restart(cluster, 'a', reboot=False)
        cluster.advance(15)
        self.assertNotIn(manual.BACK, self.bot.text(call))
        status, page = self.press(cluster, url)
        self.assertEqual(status, 202, f'после рестарта агента ссылка не работает: {status} {page}')
        self.assert_started_once(cluster)
        self.outage_message()

    def test_failed_first_message_is_sent_again(self):
        cluster = self.exits()
        self.bot.fail_sends = True
        self.silent(cluster)
        cluster.advance(30)
        self.assertTrue([call for call in self.bot.calls if call['method'] == 'sendMessage'],
                        'первое сообщение не пытались отправить')
        self.assertEqual(self.bot.sent(manual.ADMIN), [])
        self.bot.fail_sends = False
        cluster.advance(30)
        call, url = self.outage_message()
        self.assertRegex(call['data']['text'], r'\d+\s*мин')
        status, page = self.press(cluster, url)
        self.assertEqual(status, 202, f'{status} {page}')
        self.assert_started_once(cluster)


# Only these edges: the inherited cases run in test_emergency_manual_start.
for _name in [name for name in dir(manual.ManualStartTests) if name.startswith('test_')]:
    setattr(ManualStartEdgeTests, _name, None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
