"""Аварийный режим глазами оператора и клиента: настоящие агенты экзитов на
общих виртуальных часах, очередь и панель настоящие, подменены только сеть,
Hysteria2, HTTPS-сервер панели и Bot API."""

import json
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))
from vpn2.emergency import agent, notices, snapshot, spool, web  # noqa: E402
from vpn2.notify import Reply  # noqa: E402

KEY = '11' * 32
ADMIN = 900


class FakeHy2:
    def __init__(self):
        self.state = {}
        self.fail = False

    def home(self, directory):
        return str(directory)

    def read_state(self, directory):
        return self.state.get(str(directory), {'active': False, 'generation': -1})

    def start(self, directory, generation, devices):
        if self.fail:
            raise RuntimeError('no hysteria')
        self.state[str(directory)] = {'active': True, 'generation': generation}
        return {device['id']: {'uri': f"hysteria2://g{generation}-d{device['id']}"} for device in devices}

    def stop(self, directory):
        self.state[str(directory)] = {'active': False, 'generation': -1}

    def active(self, directory):
        return self.read_state(directory)['active']

    connections = poll_sessions = refresh_tls = staticmethod(lambda *_, **__: {})
    settings = staticmethod(lambda *_: {'port': 18443})
    listening = staticmethod(lambda _port: False)


class Cluster:
    def __init__(self, test, exits=3, installation='inst-1'):
        self.root = Path(tempfile.mkdtemp())
        self.now, self.wall = 1000.0, 1_800_000_000.0
        self.names = [f'exit{i}' for i in range(1, exits + 1)]
        self.nodes = {agent.ENTRY: {'host': '192.0.2.1', 'port': 51821},
                      **{name: {'host': f'192.0.2.{10 + i}', 'port': 51821} for i, name in enumerate(self.names)}}
        self.hy2, self.sent, self.telegram = FakeHy2(), [], True
        self.entry, self.ready, self.down, self.cut, self.exits = True, True, set(), set(), {}
        api = types.SimpleNamespace(call=self.call)
        for target, value in (('hy2', self.hy2), ('BotApi', lambda _token: api)):
            patcher = patch.object(agent, target, value)
            patcher.start()
            test.addCleanup(patcher.stop)
        for target, value in ((agent.web, 'make_server'), (agent.readiness, 'reachable')):
            patcher = patch.object(target, value, self.server if value == 'make_server' else
                                   lambda *_, **__: self.entry and self.ready)
            patcher.start()
            test.addCleanup(patcher.stop)
        for name in self.names:
            self.boot(name, installation)

    def boot(self, name, installation='inst-1'):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        image = dict(schema=1, version=1, installation_id=installation, created_at='2026-01-01T00:00:00+00:00',
                     admin_chat_id=ADMIN, bot_token='1:token',
                     exits=[dict(id=n, name=n.upper(), ip=self.nodes[n]['host'], priority=i)
                            for i, n in enumerate(self.names)],
                     clients=[dict(id=1, name='Аня', telegram_chat_id=101, allowed=True, expires_at=None,
                                   devices=[dict(id=11, name='телефон')]),
                              dict(id=2, name='Боря', telegram_chat_id=102, allowed=False, expires_at=None,
                                   devices=[dict(id=21, name='ноутбук')])])
        (directory / snapshot.NAME).write_bytes(snapshot.encode(image))
        config = dict(installation_id=installation, key=KEY, node=name, nodes=self.nodes)
        self.exits[name] = agent.Exit(config, directory, clock=lambda: self.now, wall=lambda: self.wall)
        self.down.discard(name)

    def server(self, directory, *, exit_id, public_ip, **providers):
        return types.SimpleNamespace(panel=web.Panel(directory, exit_id, public_ip, **providers),
                                     serve_forever=lambda: None, shutdown=lambda: None,
                                     server_close=lambda: None, refresh_tls=lambda: False)

    def call(self, chat, part):
        if not self.telegram:
            return Reply(False, 'Telegram недоступен или ответ не подтверждён')
        self.sent.append((chat, part))
        return Reply(True, result={'message_id': len(self.sent)})

    def linked(self, a, b):
        return not ({a, b} & self.down) and frozenset((a, b)) not in self.cut

    def run(self, seconds, check=None):
        for _ in range(int(seconds)):
            self.now += 1
            self.wall += 1
            up = [name for name in self.names if name not in self.down]
            if self.entry and self.now % agent.PERIOD == 0:
                raw = agent.pack(bytes.fromhex(KEY), dict(ready=self.ready, f=agent.ENTRY, n=self.stamp(),
                                                          i=self.exits[up[0]].installation))
                for name in up:
                    self.exits[name].on_packet(raw, (self.nodes[agent.ENTRY]['host'], 1), self.now)
            for name in up:
                node = self.exits[name]
                sending = node.tick(self.now)
                node.save()
                if sending:
                    raw = agent.pack(node.key, dict(node.status(self.now), i=node.installation, f=name,
                                                    n=node.stamp()))
                    for other in up:
                        if other != name and self.linked(name, other):
                            self.exits[other].on_packet(raw, (self.nodes[name]['host'], 1), self.now)
            for name in up:
                self.exits[name].work(self.now)
            leaders = [name for name in up if self.exits[name].role and not self.exits[name].autonomous]
            assert len(leaders) <= 1, f'two leaders with a majority: {leaders}'
            if check and check():
                return True
        return False

    def stamp(self):
        return self.exits[self.names[0]].stamp()

    def active(self):
        return [name for name in self.names if name not in self.down and self.exits[name].role
                and self.exits[name].leader['active'] and self.hy2.active(self.root / name)]

    def texts(self, chat):
        return [part.get('text', part.get('document', '')) for to, part in self.sent if to == chat]


class EmergencyAgentTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def outage(self, cluster):
        cluster.entry = False
        self.assertTrue(cluster.run(agent.OUTAGE + 120, lambda: cluster.active()))

    def test_entry_silent_twenty_minutes_first_exit_takes_over(self):
        cluster = Cluster(self)
        cluster.run(300)
        self.assertEqual(cluster.active(), [])
        started = cluster.now
        cluster.entry = False
        cluster.run(agent.NOTICE_AFTER - 20)
        self.assertEqual(cluster.texts(ADMIN), [])
        cluster.run(60)
        chat, part = cluster.sent[0]
        self.assertEqual((chat, len(cluster.sent)), (ADMIN, 1))
        self.assertRegex(part['text'], r'^Entry недоступен\. Аварийный режим включится через 1[67] мин$')
        button = part['reply_markup']['inline_keyboard'][0][0]
        self.assertEqual(button['text'], 'Запустить сейчас')
        self.assertTrue(button['url'].startswith('https://192.0.2.10/start/'))
        self.assertFalse(cluster.run(agent.OUTAGE - 260, lambda: cluster.active()))
        self.assertTrue(cluster.run(120, lambda: cluster.active()))
        self.assertLessEqual(cluster.now - started, agent.OUTAGE + agent.PERIOD + agent.MISSED)
        self.assertEqual(cluster.active(), ['exit1'])
        cluster.run(10)
        admin = cluster.texts(ADMIN)[-1]
        password = json.loads((cluster.root / 'exit1/web-auth.json').read_text())['password']
        self.assertIn(f'Аварийная панель запущена на EXIT1. Адрес: https://192.0.2.10. Пароль: {password}.', admin)
        self.assertRegex(admin, r'\n\nУзел: EXIT1\. Время выдачи: \d\d:\d\d \d\d\.\d\d\.\d{4} \(UTC\+03:00\)$')
        first, second = cluster.texts(101)
        self.assertIn('Аварийный конфиг Hysteria2 для устройства телефон', first)
        self.assertIn('https://192.0.2.10/instructions', first)
        self.assertTrue(second.startswith('hysteria2://g1-d11'))
        self.assertEqual(cluster.texts(102), [])
        row = spool.rows(cluster.root / 'exit1', client=2)[0]
        self.assertEqual((row['state'], row['reason']), ('blocked', 'клиент заморожен'))
        panel = cluster.exits['exit1'].server.panel
        self.assertEqual(panel.login(password)[0], 200)
        view = panel.view()
        self.assertEqual((view['exit_name'], view['delivery']['sent'], view['delivery']['denied']), ('EXIT1', 1, 1))
        self.assertEqual(cluster.exits['exit2'].server.panel.redirect, 'https://192.0.2.10')
        state = json.loads((cluster.root / 'exit1/agent.json').read_text())
        self.assertIs(state['leader']['active'], True)

    def test_entry_back_before_emergency_edits_notice(self):
        cluster = Cluster(self)
        cluster.entry = False
        cluster.run(agent.NOTICE_AFTER + 40)
        self.assertEqual(len(cluster.sent), 1)
        cluster.entry = True
        cluster.run(60)
        chat, part = cluster.sent[-1]
        self.assertEqual((part['method'], part['text'], part['message_id']),
                         ('editMessageText', notices.OUTAGE_BACK, 1))
        cluster.run(agent.OUTAGE)
        self.assertEqual((cluster.active(), len(cluster.sent)), ([], 2))
        self.assertIsNone(cluster.exits['exit1'].server)

    def test_start_link_starts_at_once_and_only_once(self):
        cluster = Cluster(self)
        cluster.entry = False
        cluster.run(agent.NOTICE_AFTER + 40)
        token = cluster.sent[0][1]['reply_markup']['inline_keyboard'][0][0]['url'].rsplit('/', 1)[1]
        leader = cluster.exits['exit1']
        self.assertEqual(leader.start_link(token[:-1] + ('0' if token[-1] != '0' else '1')), 'invalid')
        self.assertEqual(leader.start_link(token), 'starting')
        self.assertEqual(leader.start_link(token), 'gone')
        self.assertTrue(cluster.run(10, lambda: cluster.active()))
        self.assertEqual(leader.start_link(token), 'gone')
        self.assertIn('Аварийная панель запущена', cluster.texts(ADMIN)[-1])

    def test_leader_lost_next_exit_within_75_seconds_no_ping_pong(self):
        cluster = Cluster(self)
        self.outage(cluster)
        cluster.run(30)
        cluster.down.add('exit1')
        lost = cluster.now
        self.assertTrue(cluster.run(200, lambda: cluster.active() == ['exit2']))
        self.assertLessEqual(cluster.now - lost, 75 + 2)
        cluster.run(10)
        self.assertTrue(cluster.texts(ADMIN)[-1].startswith('Аварийный сервер сменился — новые адрес и пароль. '
                                                            'Основной VPN недоступен. Аварийная панель запущена '
                                                            'на EXIT2. Адрес: https://192.0.2.11.'))
        self.assertIn('Аварийный сервер сменился. Для устройства телефон', cluster.texts(101)[-2])
        self.assertTrue(cluster.texts(101)[-1].startswith('hysteria2://g2-d11'))
        # Прежний лидер вернулся: лидер не меняется, его старые учётки живут ещё TRANSITION.
        cluster.boot('exit1')
        count = len(cluster.sent)
        cluster.run(agent.TRANSITION - 30)
        self.assertEqual((cluster.active(), len(cluster.sent)), (['exit2'], count))
        self.assertTrue(cluster.hy2.active(cluster.root / 'exit1'))
        self.assertEqual(cluster.exits['exit1'].server.panel.redirect, 'https://192.0.2.11')
        cluster.run(agent.LEASE + 60)
        self.assertFalse(cluster.hy2.active(cluster.root / 'exit1'))
        self.assertEqual(cluster.active(), ['exit2'])

    def test_flapping_link_never_gives_two_leaders(self):
        cluster = Cluster(self)
        self.outage(cluster)
        for _ in range(12):
            cluster.cut = {frozenset(('exit1', 'exit2')), frozenset(('exit1', 'exit3'))}
            cluster.run(50)
            cluster.cut = set()
            cluster.run(40)
        cluster.run(120)
        self.assertEqual(len(cluster.active()), 1)

    def test_all_exits_start_at_once_one_leader(self):
        cluster = Cluster(self)
        self.outage(cluster)
        for name in cluster.names:
            cluster.boot(name)
        cluster.entry = False
        self.assertTrue(cluster.run(agent.LEASE + 30, lambda: cluster.active()))
        cluster.run(120)
        self.assertEqual(cluster.active(), ['exit1'])
        # Тот же лидер продолжил ту же выдачу: новых сообщений клиенту нет.
        self.assertEqual(len(cluster.texts(101)), 2)

    def test_return_announced_then_emergency_ends_at_deadline(self):
        cluster = Cluster(self)
        self.outage(cluster)
        cluster.run(30)
        cluster.entry = True
        back = cluster.now
        self.assertTrue(cluster.run(300, lambda: cluster.exits['exit1'].leader.get('deadline')))
        self.assertLessEqual(cluster.now - back, agent.STABLE + agent.PERIOD + 2)
        deadline = cluster.exits['exit1'].leader['deadline']
        self.assertEqual(deadline % 60, 0)
        self.assertTrue(agent.RETURN <= deadline - cluster.wall < agent.RETURN + 60)
        cluster.run(5)
        at = notices.clock(deadline)
        self.assertEqual(cluster.texts(ADMIN)[-1].split('\n\n')[0],
                         f'Основной VPN восстановлен. Аварийный режим будет завершён в {at}, примерно через 10 минут')
        self.assertIn(f'Аварийный доступ будет отключён в {at}, примерно через 10 минут', cluster.texts(101)[-1])
        self.assertEqual(cluster.exits['exit1'].server.panel.view()['deadline'], deadline)
        cluster.run(deadline - cluster.wall - 2)
        self.assertEqual(cluster.active(), ['exit1'])
        cluster.run(10)
        self.assertEqual(cluster.active(), [])
        for name in cluster.names:
            self.assertFalse(cluster.hy2.active(cluster.root / name))
            self.assertIs(cluster.exits[name].leader['active'], False)
            self.assertIsNone(cluster.exits[name].server)
        count = len(cluster.sent)
        cluster.run(600)
        self.assertEqual((cluster.active(), len(cluster.sent)), ([], count))

    def test_return_cancelled_when_entry_fails_again(self):
        cluster = Cluster(self)
        self.outage(cluster)
        cluster.entry = True
        cluster.run(agent.STABLE + 60)
        self.assertIsNotNone(cluster.exits['exit1'].leader['deadline'])
        cluster.ready = False
        cluster.run(agent.MISSED)
        self.assertIsNone(cluster.exits['exit1'].leader['deadline'])
        self.assertTrue(cluster.texts(ADMIN)[-1].startswith('Основной VPN снова недоступен. Завершение аварийного '
                                                            'режима отменено'))
        self.assertTrue(cluster.texts(101)[-1].startswith('Возврат отменён: основное VPN-подключение снова '
                                                          'недоступно.'))
        self.assertTrue(cluster.exits['exit1'].server.panel.view()['cancelled'])
        cluster.run(agent.RETURN + 120)
        self.assertEqual(cluster.active(), ['exit1'])

    def test_isolated_exit_leads_alone_after_guard(self):
        cluster = Cluster(self)
        cluster.entry = False
        cluster.cut = {frozenset(('exit3', 'exit1')), frozenset(('exit3', 'exit2'))}
        cluster.run(agent.MISSED + agent.GUARD - 20)
        self.assertFalse(cluster.exits['exit3'].role)
        cluster.run(agent.OUTAGE)
        lone = cluster.exits['exit3']
        self.assertTrue(lone.role and lone.autonomous and lone.leader['active'])
        self.assertTrue(lone.server.panel.view()['autonomous'])
        self.assertEqual(cluster.active(), ['exit1', 'exit3'])

    def test_single_exit_and_telegram_outage_delivers_later(self):
        cluster = Cluster(self, exits=1)
        cluster.telegram = False
        self.outage(cluster)
        cluster.run(60)
        self.assertEqual(cluster.sent, [])
        self.assertFalse(cluster.exits['exit1'].autonomous)
        cluster.telegram = True
        cluster.run(spool.RETRY_MAX)
        self.assertEqual(len(cluster.texts(101)), 2)

    def test_failed_issuance_is_retried(self):
        cluster = Cluster(self)
        cluster.hy2.fail = True
        cluster.entry = False
        cluster.run(agent.OUTAGE + 120)
        self.assertEqual((cluster.active(), cluster.texts(101)), ([], []))
        cluster.hy2.fail = False
        cluster.run(40)
        self.assertEqual(cluster.active(), ['exit1'])

    def test_reinstalled_entry_ends_old_emergency(self):
        cluster = Cluster(self)
        self.outage(cluster)
        cluster.run(30)
        count = len(cluster.texts(101))
        for name in cluster.names:
            cluster.boot(name, 'inst-2')
        cluster.entry = True
        cluster.run(30)
        self.assertEqual(cluster.active(), [])
        self.assertFalse(cluster.hy2.active(cluster.root / 'exit1'))
        self.assertEqual(len(cluster.texts(101)), count + 1)
        self.assertTrue(cluster.texts(101)[-1].startswith('Основное VPN-подключение восстановлено. Аварийный '
                                                          'доступ уже отключён'))
        cluster.run(agent.OUTAGE)
        self.assertEqual(len(cluster.texts(101)), count + 1)

    def test_forged_and_replayed_packets_are_ignored(self):
        cluster = Cluster(self)
        node = cluster.exits['exit1']
        cluster.entry = False
        cluster.run(agent.MISSED + 30)
        good = agent.pack(node.key, dict(ready=True, f=agent.ENTRY, n=cluster.stamp(), i='inst-1'))
        for raw in (b'x' * 40, agent.pack(b'k' * 32, dict(ready=True, f=agent.ENTRY, n=cluster.stamp(), i='inst-1')),
                    agent.pack(node.key, dict(ready=True, f=agent.ENTRY, n=1, i='inst-1')),
                    agent.pack(node.key, dict(ready=True, f=agent.ENTRY, n=cluster.stamp(), i='other'))):
            node.on_packet(raw, ('192.0.2.1', 1), cluster.now)
        self.assertGreater(cluster.now - node.entry_seen, agent.MISSED)
        node.on_packet(good, ('192.0.2.1', 1), cluster.now)
        self.assertEqual(node.entry_seen, cluster.now)
        seen = node.entry_seen
        cluster.now += 5
        node.on_packet(good, ('192.0.2.1', 1), cluster.now)
        self.assertEqual(node.entry_seen, seen)


if __name__ == '__main__':
    unittest.main()
