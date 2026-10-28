"""VPN2-565: before the emergency the server on 443 takes only the start link.

A panel password left on disk by an earlier emergency must not open a session
or a broadcast while the server is up only for the admin's link. Same harness
as test_emergency_manual_start: real agents over UDP, a fake Bot API, HTTPS to
the exit the button names.
"""

import hashlib
import http.client
import json
import ssl
import types
import unittest
from urllib.parse import urlsplit

import test_emergency_manual_start as manual

EARLIER = 'earlier-emergency'
CLIENT_CHAT = manual.ADMIN + 1000


class LinkOnlyPostTests(manual.ManualStartTests):
    def post(self, cluster, url, path, body, cookie=None):
        """POST same-origin JSON to the exit the link names: (status, Set-Cookie)."""
        host = urlsplit(url).hostname
        node = next(node for node, agent in cluster.exit_agents().items() if agent.addresses[node][0] == host)
        server = cluster.agents[node].lifecycle.server
        self.assertIsNotNone(server, 'по ссылке никто не отвечает')
        connection = http.client.HTTPSConnection(
            host, server.server_address[1], timeout=5,
            context=ssl._create_unverified_context())  # noqa: S323 - local self-signed panel
        headers = {'Host': host, 'Origin': f'https://{host}', 'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        try:
            connection.request('POST', path, body=json.dumps(body).encode(), headers=headers)
            response = connection.getresponse()
            response.read()
            return response.status, response.getheader('Set-Cookie')
        finally:
            connection.close()

    @staticmethod
    def port_busy(*_args, **_kwargs):
        raise OSError(98, 'Address already in use')

    def assert_panel_closed(self, cluster, url, password):
        status, cookie = self.post(cluster, url, '/api/login', {'password': password})
        self.assertNotEqual(status, 200, 'до аварии вход в панель по старому паролю принят')
        self.assertFalse(cookie and 'vpn2-emergency=' in cookie and 'Max-Age=0' not in cookie,
                         f'до аварии выдана сессия: {cookie}')
        session = cookie.split(';', 1)[0] if cookie else '__Host-vpn2-emergency=forged'
        status, _cookie = self.post(cluster, url, '/api/broadcast', {'text': 'рассылка до аварии'}, session)
        self.assertFalse(200 <= status < 300, f'до аварии рассылка принята: HTTP {status}')
        cluster.advance(60)
        self.assertEqual(self.bot.sent(CLIENT_CHAT), [], 'до аварии клиентам ушла рассылка')

    def test_stale_password_on_disk_opens_nothing_before_emergency(self):
        cluster = self.exits()
        auth = cluster.agents['a'].lifecycle.directory / 'web-auth.json'
        auth.write_text(json.dumps(dict(
            emergency_id='earlier', generation=1, password=EARLIER,
            digest=hashlib.sha256(EARLIER.encode()).hexdigest(), attempts={})))
        self.silent(cluster)
        _call, url = self.outage_message()
        self.assertEqual(cluster.agents['a'].lifecycle.server.panel.link_only, True)
        self.assert_panel_closed(cluster, url, EARLIER)
        self.assertEqual(cluster.leaders(), [])
        self.assertEqual(cluster.starts(), 0)

    def test_emergency_ended_while_panel_down_leaves_no_password(self):
        """The reviewer's case: the leader reboots during the emergency and the
        emergency ends before its panel is back; the entry then falls silent again."""
        cluster = self.exits()
        self.started_by_link(cluster)
        self.assertEqual(cluster.leaders(), ['a'])
        lifecycle = cluster.agents['a'].lifecycle
        password = lifecycle.server.panel.auth['password']
        # Down: the agent stops, its panel with it.
        lifecycle.close()
        cluster.stop('a')
        cluster.start('entry')
        cluster.drain()
        for _ in range(400):
            if not cluster.active():
                break
            cluster.advance(15)
        self.assertEqual(cluster.active(), [], 'авария не кончилась')
        # Back after the end: the agent still holds the emergency and later
        # finishes it on its own, with no panel up.
        cluster.start('a')
        lifecycle = cluster.agents['a'].lifecycle
        web_module = lifecycle.web
        lifecycle.web = types.SimpleNamespace(make_server=self.port_busy)
        with self.assertLogs('vpn2_emergency.lifecycle', 'ERROR'):
            cluster.drain()
            for _ in range(400):
                if not cluster.active():
                    break
                cluster.advance(15)
        self.assertEqual(cluster.active(), [], 'вернувшийся экзит не закончил аварию')
        self.assertIsNone(lifecycle.server)
        lifecycle.web = web_module
        self.bot.calls.clear()
        self.silent(cluster)
        _call, url = self.outage_message()
        self.assertTrue(cluster.agents['a'].lifecycle.server.panel.link_only)
        self.assert_panel_closed(cluster, url, password)


# Only these cases: the inherited ones run in test_emergency_manual_start.
for _name in [name for name in dir(manual.ManualStartTests) if name.startswith('test_')]:
    setattr(LinkOnlyPostTests, _name, None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
