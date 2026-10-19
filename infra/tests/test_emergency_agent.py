"""Real UDP state machines driven by a monotonic test clock; no sleeping.

The same assertions can target an older tree using VPN2_TEST_ROOT.
"""

import importlib.util
import inspect
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'infra/emergency'))

from vpn2_emergency import hooks, lifecycle, tgqueue, web  # noqa: E402
from vpn2_emergency.agent import Agent  # noqa: E402
from vpn2_emergency.heartbeat import PERIOD, Heartbeat, decode, encode  # noqa: E402
from vpn2_emergency.leader import GUARD, LEASE, TRANSITION, Leader  # noqa: E402
from vpn2_emergency.state import MAX_STATE_BYTES, State  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeHy2:
    """Hysteria2 of one exit: survives agent restarts like the real unit."""

    def __init__(self):
        self.starts = []
        self.running = False

    def start(self, _state_dir, generation, devices, options=None):
        del devices, options
        self.starts.append(generation)
        self.running = True
        return {}

    def stop(self, _state_dir):
        self.running = False

    def settings(self, _options):
        return {'port': 18443}

    def listening(self, _port):
        return self.running

    def watch(self, _state_dir):
        return types.SimpleNamespace(stop=lambda: None)

    def revoked(self, _state_dir, _generation):
        return False


class FakeTelegram:
    """Local queue of one exit: return notices go out only with a deadline."""

    def __init__(self):
        self.rows, self.sent, self.cancelled = [], [], []

    def _enqueue(self, audience, *_args, emergency_id='', bundle_version=0, event='initial', **_kwargs):
        self.rows.append(dict(audience=audience, emergency=emergency_id, version=str(bundle_version),
                              event=event, state='pending', sent=0))

    def enqueue_admin(self, *args, **kwargs):
        self._enqueue('admin', *args, **kwargs)

    def enqueue_clients(self, *args, **kwargs):
        self._enqueue('client', *args, **kwargs)

    def run_once(self, *_args, deadline=None, **_kwargs):
        for row in self.rows:
            if row['state'] != 'pending' or (row['event'] == 'recovered' and deadline is None):
                continue
            row.update(state='sent', sent=1)
            if row['event'] == 'recovered':
                self.sent.append((row['audience'], deadline))

    def cancel(self, *_args, events=None, **_kwargs):
        self.cancelled.append(events)
        for row in self.rows:
            if row['state'] == 'pending' and (events is None or row['event'] in events):
                row['state'] = 'cancelled'

    def delivery_status(self, _directory, audience='client'):
        return [dict(row) for row in self.rows if row['audience'] == audience]

    def prune(self, *_args, **_kwargs):
        pass

    def reachable(self, *_args, **_kwargs):
        return True


class Events:
    def __init__(self):
        self.calls = []

    def on_emergency_leader(self, *args):
        self.calls.append(('leader', args))

    def on_step_down(self, *args):
        self.calls.append(('step_down', args))

    def on_entry_recovered(self, *args):
        self.calls.append(('recovered', args))

    def on_emergency_end(self):
        self.calls.append(('end', ()))


class Cluster:
    def __init__(self, directory, count=3):
        self.directory = Path(directory)
        self.clock = Clock()
        # Wall time moves with the monotonic clock; deadlines are wall time.
        self.wall = lambda: 1_800_000_000 + self.clock.now
        self.blocked = set()
        self.agents = {}
        self.events = {}
        self.configs = {}
        self.exits = [{'id': chr(97 + i), 'priority': i, 'name': chr(97 + i), 'ip': '127.0.0.1'}
                      for i in range(count)]
        reservations = []
        addresses = {}
        for node in ['entry', *(item['id'] for item in self.exits)]:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(('127.0.0.1', 0))
            reservations.append(sock)
            addresses[node] = {'host': '127.0.0.1', 'port': sock.getsockname()[1]}
        for sock in reservations:
            sock.close()
        for node in addresses:
            self.configs[node] = dict(node=node, installation_id='fixture', key='ab' * 32,
                                      bind='127.0.0.1', nodes=addresses)
            self.start(node)
        self.drain()

    def wall_clock(self):
        return {'wall': self.wall} if 'wall' in inspect.signature(Agent).parameters else {}

    def snapshot(self, _directory):
        return {'installation_id': 'fixture', 'exits': self.exits}

    def start(self, node):
        agent = Agent(self.configs[node], self.directory / node, clock=self.clock,
                      snapshot_loader=self.snapshot, **self.wall_clock())
        original_send = agent.send

        def send(peer, message, *address):
            if (node, peer) not in self.blocked:
                original_send(peer, message, *address)

        agent.send = send
        self.agents[node] = agent
        self.events.setdefault(node, Events())
        agent.tick(self.clock())
        if agent.leader:
            agent.leader.events = self.events[node]

    def stop(self, node):
        self.agents.pop(node).close()

    def close(self):
        for agent in self.agents.values():
            agent.close()

    def drain(self):
        for _ in range(10):
            received = 0
            for agent in self.agents.values():
                while True:
                    try:
                        packet, address = agent.sock.recvfrom(16385)
                    except BlockingIOError:
                        break
                    agent.receive(packet, address, self.clock())
                    received += 1
            if not received:
                break

    def advance(self, seconds=15):
        for _ in range(seconds // 15):
            self.clock.now += 15
            for agent in self.agents.values():
                agent.tick(self.clock())
            self.drain()

    def leaders(self):
        return [node for node, agent in self.agents.items()
                if agent.leader and agent.leader.data['role'] == 'leader']


class Network(Cluster):
    """Agents tick on their own phase like Agent.run and every datagram takes
    one step to arrive, so lease expiry meets real round boundaries. Role
    lines are recorded with the clock at the moment they are logged."""

    STEP = 0.25

    def __init__(self, directory, count=3, phases=None):
        self.queue = []
        self.next_tick = {}
        self.journal = []
        self.current = None
        self.phases = phases or {}
        handler = logging.Handler(logging.INFO)
        handler.emit = lambda record: self.journal.append(
            (self.clock(), self.current, record.getMessage()))
        self.handler = handler
        self.logger = logging.getLogger()
        self.saved = self.logger.level, self.logger.handlers[:]
        self.logger.setLevel(logging.INFO)
        self.logger.handlers[:] = [handler]
        super().__init__(directory, count)
        for node in self.agents:
            self.next_tick[node] = self.clock() + self.phases.get(node, 0)

    def close(self):
        self.logger.setLevel(self.saved[0])
        self.logger.handlers[:] = self.saved[1]
        super().close()

    def start(self, node):
        agent = Agent(self.configs[node], self.directory / node, clock=self.clock,
                      snapshot_loader=self.snapshot, **self.wall_clock())

        def send(peer, message, _address=None):
            if (node, peer) in self.blocked:
                return
            packet = encode(agent.key, message | {'installation_id': agent.installation, 'sender': node})
            self.queue.append((self.clock() + self.STEP, peer, packet, agent.addresses[node]))

        agent.send = send
        self.agents[node] = agent
        self.events.setdefault(node, Events())
        self.current = node
        agent.tick(self.clock())
        self.next_tick[node] = self.clock() + 15
        if agent.leader:
            agent.leader.events = self.events[node]

    def step(self):
        self.clock.now += self.STEP
        now = self.clock()
        due = [item for item in self.queue if item[0] <= now]
        self.queue = [item for item in self.queue if item[0] > now]
        for _at, peer, packet, address in due:
            if peer in self.agents:
                self.current = peer
                self.agents[peer].receive(packet, address, now)
        for node, agent in list(self.agents.items()):
            if now >= self.next_tick[node]:
                self.current = node
                agent.tick(now)
                self.next_tick[node] = now + 15

    def run(self, seconds, check=None):
        end = self.clock() + seconds
        while self.clock() < end:
            self.step()
            if check:
                check()

    def announced(self, since, role='leader'):
        return [(at, node) for at, node, line in self.journal
                if at >= since and line.startswith(f'role={role} ')]


class EmergencyAgentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='emergency-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def cluster(self, count=3):
        cluster = Cluster(self.root, count)
        self.addCleanup(cluster.close)
        return cluster

    def test_authentication_installation_nonce_and_packet_limit(self):
        packet = encode(b'k', {'value': 1})
        self.assertEqual(decode(b'k', packet), {'value': 1})
        with self.assertRaises(ValueError):
            decode(b'x', packet)
        with self.assertRaises(ValueError):
            encode(b'k', {'large': 'a' * 16384})
        cluster = self.cluster()
        agent = cluster.agents['a']
        before = agent.heartbeat.last_entry
        message = dict(sender='entry', installation_id='other', kind='reply', nonce='a' * 48,
                       evidence=dict(epoch=99, run='forged', sequence=1, age=0))
        for installation in ('other', 'fixture'):
            message['installation_id'] = installation
            agent.receive(encode(agent.key, message), agent.addresses['entry'], cluster.clock() + 300)
        self.assertEqual(agent.heartbeat.last_entry, before)
        self.assertNotEqual(agent.state.data['entry_cursor'][1], 'forged')

    def test_heartbeat_no_client_traffic_for_thirty_minutes(self):
        cluster = self.cluster()
        cluster.advance(1800)
        self.assertEqual(cluster.leaders(), [])
        self.assertTrue(all(not agent.heartbeat.emergency_due(cluster.clock())
                            for node, agent in cluster.agents.items() if node != 'entry'))

    def test_one_exit_uses_only_direct_neighbor_evidence(self):
        cluster = self.cluster()
        cluster.blocked.add(('entry', 'a'))
        cluster.advance(1800)
        self.assertEqual(cluster.leaders(), [])
        self.assertTrue(cluster.agents['a'].heartbeat.poll_due(cluster.clock()))
        self.assertFalse(cluster.agents['a'].heartbeat.emergency_due(cluster.clock()))
        self.assertGreater(cluster.agents['a'].heartbeat.direct_evidence(cluster.clock())['age'], 1200)

    def test_stale_forwarding_restart_and_new_entry_run(self):
        state = State(self.root, clock_boot='boot')
        hb = Heartbeat(state, 1000)
        evidence = dict(epoch=1, run='entry-1', sequence=1, age=0)
        self.assertTrue(hb.receive(evidence, 1015, direct=True))
        state.save()
        hb = Heartbeat(State(self.root, clock_boot='boot'), 1900)
        for observer in ('a', 'b', 'c'):
            hb.receive(evidence, 1900, observer=observer)
        self.assertEqual(hb.last_entry, 1015)
        self.assertTrue(hb.emergency_due(2215))
        self.assertFalse(hb.receive(dict(evidence, run='other'), 2215))
        self.assertTrue(hb.receive(dict(evidence, epoch=2, run='entry-2'), 2230, direct=True))
        self.assertFalse(hb.receive(dict(evidence, sequence=100000), 2245))
        self.assertEqual(hb.last_entry, 2230)
        hb.challenge('a', 2245)
        self.assertIsNone(hb.response('a', 'wrong', 2245))
        nonce = hb.challenge('a', 2245)
        self.assertEqual(hb.response('a', nonce, 2246), 1)
        self.assertIsNone(hb.response('a', nonce, 2246))

    def test_peer_poll_starts_after_three_missed_heartbeats(self):
        state = State(self.root, clock_boot='boot')
        hb = Heartbeat(state, 1000)
        self.assertFalse(hb.poll_due(1000))
        self.assertFalse(hb.poll_due(1044))
        self.assertTrue(hb.poll_due(1045))
        state.save()
        hb = Heartbeat(State(self.root, clock_boot='boot'), 1040)
        self.assertFalse(hb.poll_due(1044))
        self.assertTrue(hb.poll_due(1045))

    def test_outage_threshold_simultaneous_start_and_failover(self):
        cluster = self.cluster()
        cluster.stop('entry')
        cluster.advance(1185)
        self.assertEqual(cluster.leaders(), [])
        self.assertTrue(all(not agent.leader.data['active'] for agent in cluster.agents.values()))
        for _ in range(8):
            cluster.advance()
            self.assertLessEqual(len(cluster.leaders()), 1)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertEqual(sum(call[0] == 'leader' for event in cluster.events.values()
                             for call in event.calls), 1)
        cluster.stop('a')
        started = cluster.clock()
        while not cluster.leaders() and cluster.clock() - started < 90:
            cluster.advance()
        self.assertEqual(cluster.leaders(), ['b'])
        self.assertLessEqual(cluster.clock() - started, 90)
        self.assertEqual(sum(call[0] == 'leader' for call in cluster.events['b'].calls), 1)
        cluster.start('a')
        cluster.advance(90)
        self.assertEqual(cluster.leaders(), ['b'])

    def test_followers_receive_redirect_and_target_loss_events(self):
        cluster = self.cluster()
        cluster.stop('entry')
        cluster.advance(1320)
        self.assertIn(('step_down', ('a',)), cluster.events['b'].calls)
        self.assertIn(('step_down', ('a',)), cluster.events['c'].calls)
        for peer in ('a', 'c'):
            cluster.blocked.update({('b', peer), (peer, 'b')})
        cluster.advance(90)
        self.assertIn(('step_down', (None,)), cluster.events['b'].calls)
        self.assertIsNone(cluster.agents['b'].leader.data['redirect'])

    def test_partition_guard_merge_retirement_and_no_redirect_cycle(self):
        cluster = self.cluster()
        cluster.stop('entry')
        cluster.advance(1320)
        self.assertEqual(cluster.leaders(), ['a'])
        for node in ('b', 'c'):
            cluster.blocked.update({('a', node), (node, 'a')})
        cluster.advance(90)
        self.assertEqual(cluster.leaders(), ['b'])
        cluster.advance(195)
        self.assertEqual(cluster.leaders(), ['b'])
        cluster.advance(105)
        self.assertEqual(set(cluster.leaders()), {'a', 'b'})
        self.assertTrue(cluster.agents['a'].leader.data['autonomous'])
        cluster.blocked.clear()
        cluster.advance(45)
        self.assertEqual(cluster.leaders(), ['b'])
        self.assertEqual(cluster.agents['a'].leader.data['redirect'], 'b')
        self.assertIsNone(cluster.agents['b'].leader.data['redirect'])
        self.assertNotIn('retire_at', cluster.agents['a'].leader.data)
        issued_at = cluster.clock()
        cluster.agents['b'].leader.bundle_ready(issued_at)
        cluster.advance(30)
        deadline = cluster.agents['a'].leader.data['retire_at']
        self.assertLessEqual(deadline, issued_at + 600)
        cluster.advance(555)
        self.assertLessEqual(cluster.clock(), deadline)
        cluster.advance(30)
        self.assertEqual(sum(call[0] == 'end' for call in cluster.events['a'].calls), 1)

    def test_merge_does_not_restart_old_bundle_transition(self):
        cluster = self.cluster()
        cluster.stop('entry')
        cluster.advance(1320)
        loser = cluster.agents['a'].leader
        winner = cluster.agents['b'].leader
        loser.data['autonomous'] = True
        winner.data.update(role='leader', owner='b', autonomous=False)
        winner.bundle_ready(cluster.clock() - 300)
        winner.bundle_ready(cluster.clock())
        loser.observe('b', winner.status(cluster.clock()), cluster.clock(), transit=5)
        loser.tick(cluster.clock(), True)
        self.assertEqual(loser.data['redirect'], 'b')
        self.assertLessEqual(loser.data['retire_at'], cluster.clock() + 295)
        loser.tick(cluster.clock() + 295, True)
        loser.tick(cluster.clock() + 300, True)
        self.assertEqual(sum(call[0] == 'end' for call in cluster.events['a'].calls), 1)

    def test_old_retirement_does_not_stop_a_new_leader_generation(self):
        cluster = self.cluster()
        cluster.stop('entry')
        cluster.advance(1320)
        for node in ('b', 'c'):
            cluster.blocked.update({('a', node), (node, 'a')})
        cluster.advance(390)
        cluster.agents['b'].leader.bundle_ready(cluster.clock())
        cluster.blocked.clear()
        cluster.advance(30)
        self.assertEqual(cluster.leaders(), ['b'])
        self.assertIn('retire_at', cluster.agents['a'].leader.data)
        previous_generation = cluster.agents['a'].leader.data['issued_generation']
        cluster.stop('b')
        cluster.advance(90)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertGreater(cluster.agents['a'].leader.data['issued_generation'], previous_generation)
        events_before = list(cluster.events['a'].calls)
        cluster.advance(600)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertEqual(cluster.events['a'].calls, events_before)

    def test_asymmetric_link_eventually_converges(self):
        cluster = self.cluster(2)
        cluster.stop('entry')
        cluster.blocked.add(('b', 'a'))
        cluster.advance(1485)
        self.assertEqual(cluster.leaders(), [])
        cluster.advance(45)
        self.assertEqual(set(cluster.leaders()), {'a', 'b'})
        self.assertTrue(all(agent.leader.data['autonomous'] for agent in cluster.agents.values()))
        cluster.blocked.clear()
        cluster.advance(120)
        self.assertEqual(len(cluster.leaders()), 1)
        for node, agent in cluster.agents.items():
            target = agent.leader.data.get('redirect')
            if target:
                self.assertNotEqual(cluster.agents[target].leader.data.get('redirect'), node)

    def test_restart_preserves_issuance_and_lost_credentials_rotate(self):
        cluster = self.cluster()
        cluster.stop('entry')
        cluster.advance(1320)
        before = list(cluster.events['a'].calls)
        generation = cluster.agents['a'].leader.data['issued_generation']
        cluster.stop('a')
        cluster.start('a')
        cluster.advance(30)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertEqual(cluster.events['a'].calls, before)
        cluster.agents['a'].leader.credentials_lost()
        cluster.advance()
        self.assertEqual(cluster.agents['a'].leader.data['issued_generation'], generation + 1)
        self.assertEqual(len(cluster.events['a'].calls), len(before) + 1)

    def test_unknown_neighbors_and_last_exit_require_guard(self):
        cluster = self.cluster()
        for node in ('entry', 'b', 'c'):
            cluster.stop(node)
        cluster.advance(1485)
        self.assertEqual(cluster.leaders(), [])
        cluster.advance(15)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertTrue(cluster.agents['a'].leader.data['autonomous'])

    def test_single_configured_exit_is_explicitly_autonomous(self):
        cluster = self.cluster(1)
        cluster.stop('entry')
        cluster.advance(1485)
        self.assertEqual(cluster.leaders(), [])
        cluster.advance(15)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertTrue(cluster.agents['a'].leader.data['autonomous'])

    def test_autonomous_restart_keeps_role_and_does_not_repeat_issuance(self):
        cluster = self.cluster(1)
        cluster.stop('entry')
        cluster.advance(1500)
        self.assertEqual(cluster.leaders(), ['a'])
        before = list(cluster.events['a'].calls)
        cluster.stop('a')
        cluster.start('a')
        self.assertEqual(cluster.leaders(), ['a'])
        cluster.advance(30)
        self.assertEqual(cluster.events['a'].calls, before)

    def test_replayed_candidate_poll_cannot_extend_lease(self):
        cluster = self.cluster()
        cluster.stop('entry')
        cluster.advance(1320)
        voter = cluster.agents['b'].leader
        confirmed_until = voter.peers['a'][1] + LEASE
        cluster.stop('a')
        voter.grant('a', cluster.clock() + 15, True)
        self.assertLessEqual(voter.data['grant_until'], confirmed_until)
        voter.grant('a', cluster.clock() + 90, True)
        self.assertLessEqual(voter.data['grant_until'], confirmed_until)

    def test_fresh_neighbor_does_not_adopt_partition_emergency(self):
        cluster = self.cluster()
        agent = cluster.agents['a']
        before = agent.leader.data['active']
        remote = cluster.agents['b'].leader
        remote.data.update(active=True, role='leader', owner='b', autonomous=True)
        nonce = agent.heartbeat.challenge('b', cluster.clock())
        message = dict(sender='b', installation_id='fixture', kind='reply', nonce=nonce,
                       evidence=cluster.agents['b'].heartbeat.direct_evidence(cluster.clock()),
                       status=remote.status(cluster.clock()))
        agent.receive(encode(agent.key, message), agent.addresses['b'], cluster.clock())
        self.assertEqual(agent.leader.data['active'], before)
        self.assertIn('b', agent.leader.peers)

    def test_staggered_rounds_fail_over_within_ninety_seconds(self):
        cluster = self.cluster()
        cluster.stop('entry')
        phases = {'a': 0, 'b': 7, 'c': 14}

        def second():
            cluster.clock.now += 1
            for node, agent in cluster.agents.items():
                if int(cluster.clock()) % 15 == phases[node]:
                    agent.tick(cluster.clock())
            cluster.drain()

        for _ in range(1327):
            second()
            self.assertLessEqual(len(cluster.leaders()), 1)
        self.assertEqual(len(cluster.leaders()), 1)
        cluster.stop(cluster.leaders()[0])
        for _ in range(90):
            second()
            if cluster.leaders():
                break
        self.assertEqual(len(cluster.leaders()), 1)

    def network(self):
        network = Network(self.root, 3, {'a': 0, 'b': 6.5, 'c': 11.25})
        self.addCleanup(network.close)
        network.stop('entry')
        network.run(1500)
        self.assertEqual(network.leaders(), ['a'])
        return network

    def test_failover_worst_case_from_constants_fits_ninety_seconds(self):
        # A follower honours the dead leader for LEASE after its last reply
        # and asks for votes only on its own round. The reply lands after
        # that round began, so the expiry is acted on one round later.
        worst = (LEASE // PERIOD + 1) * PERIOD
        self.assertLess(worst, 90)

    def test_leader_stop_announced_within_ninety_seconds_at_every_phase(self):
        network = self.network()
        delays = []
        for offset in [index * 1.25 for index in range(12)]:
            while (network.clock() - 1000) % 15 != offset:
                network.step()
            old = network.leaders()[0]
            network.stop(old)
            stopped = network.clock()

            def single():
                self.assertLessEqual(len(network.leaders()), 1)

            while not network.announced(stopped) and network.clock() - stopped < 120:
                network.run(Network.STEP, single)
            announced = network.announced(stopped)
            self.assertTrue(announced, f'no leader after stop at phase {offset}')
            delays.append(announced[0][0] - stopped)
            network.start(old)
            network.run(150, single)
        self.assertLessEqual(max(delays), 90, delays)

    def failover_to_b(self):
        network = self.network()
        network.stop('a')
        network.run(120)
        self.assertEqual(network.leaders(), ['b'])
        return network

    def test_returning_higher_priority_exit_stays_follower(self):
        network = self.failover_to_b()
        network.start('a')
        network.run(600, lambda: self.assertEqual(network.leaders(), ['b']))
        self.assertEqual(network.agents['a'].leader.data['role'], 'follower')
        self.assertEqual(network.agents['a'].leader.data['redirect'], 'b')

    def assert_cut_off_exit_keeps_pair_leader(self, network):
        for node in ('a', 'b'):
            network.blocked.update({('c', node), (node, 'c')})
        network.run(600, lambda: self.assertEqual(
            [node for node in network.leaders() if node != 'c'], ['b']))
        self.assertEqual(network.agents['a'].leader.data['role'], 'follower')
        self.assertEqual(set(network.leaders()), {'b', 'c'})
        self.assertTrue(network.agents['c'].leader.data['autonomous'])
        self.assertFalse(network.agents['b'].leader.data['autonomous'])

    def test_cut_off_exit_does_not_unseat_leader_of_the_other_two(self):
        network = self.failover_to_b()
        network.start('a')
        network.run(120)
        self.assert_cut_off_exit_keeps_pair_leader(network)

    def test_return_then_cut_off_keeps_leader(self):
        # Stand order: the higher-priority exit returns, two seconds later
        # the third one is cut off from both.
        network = self.failover_to_b()
        network.start('a')
        network.run(2)
        self.assert_cut_off_exit_keeps_pair_leader(network)

    def elected(self):
        exits = [{'id': node, 'priority': rank} for rank, node in enumerate('abc')]
        # Created after the reboot vote quarantine, so it may vote at once.
        leader = Leader(State(self.root, clock_boot='boot'), 'a', exits, 'fixture', 1000 - LEASE, events=Events())
        self.vote(leader, 1000, lease=LEASE)
        leader.tick(1000, True)
        self.assertEqual(leader.data['role'], 'leader')
        return leader

    def vote(self, leader, now, lease):
        leader.observe('b', {'role': 'follower', 'generation': 0, 'lease': lease, 'grant': 'a',
                             'owner': None, 'emergency_id': ''}, now)

    def test_leader_keeps_role_through_one_round_without_majority(self):
        # Under 30 % loss the only vote may expire for a round and come back.
        leader = self.elected()
        self.vote(leader, 1000, lease=1)
        leader.tick(1015, True)
        self.assertEqual(leader.data['role'], 'leader')
        self.vote(leader, 1020, lease=LEASE)
        leader.tick(1030, True)
        self.assertEqual(leader.data['role'], 'leader')
        self.assertEqual(leader.data['failed_rounds'], 0)
        self.assertNotIn('step_down', [call[0] for call in leader.events.calls])

    def test_leader_without_majority_steps_down_within_lease(self):
        leader = self.elected()
        self.vote(leader, 1000, lease=1)
        now = 1000
        while leader.data['role'] == 'leader' and now < 1000 + 2 * LEASE:
            now += PERIOD
            leader.tick(now, True)
        self.assertEqual(leader.data['role'], 'follower')
        self.assertLess(now - 1000, LEASE)
        self.assertIn(('step_down', (None,)), leader.events.calls)

    def test_leader_that_merges_logs_autonomous_false(self):
        cluster = self.cluster()
        cluster.stop('entry')
        cluster.advance(1320)
        for node in ('b', 'c'):
            cluster.blocked.update({('a', node), (node, 'a')})
        cluster.advance(390)
        self.assertTrue(cluster.agents['a'].leader.data['autonomous'])
        cluster.blocked.clear()
        with self.assertLogs(level='INFO') as logs:
            cluster.advance(45)
        self.assertEqual(cluster.leaders(), ['b'])
        self.assertFalse(cluster.agents['a'].leader.data['autonomous'])
        self.assertFalse(cluster.agents['a'].leader.status(cluster.clock())['autonomous'])
        self.assertIn('INFO:root:role=follower autonomous=false', logs.output)

    def announcer(self, cluster, node):
        """An exit with the real lifecycle and Telegram queue behind a fake Bot API."""
        from test_emergency_lifecycle import Bot, FakeHy2, FakeServer
        bot = Bot()
        thread = threading.Thread(target=bot.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        self.addCleanup(bot.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(bot.shutdown)
        snapshot = {'schema': 1, 'installation_id': 'fixture', 'bot_token': '123:fixture-only',
                    'admin_chat_id': 900, 'exits': cluster.exits, 'created_at': '2026-01-01T00:00:00Z',
                    'clients': [{'id': 1, 'name': 'first', 'telegram_chat_id': 101, 'allowed': True,
                                 'expires_at': None, 'devices': [{'id': 11, 'name': 'phone'}]}]}
        module = types.ModuleType('vpn2_emergency.snapshot')
        module.load = lambda _directory: snapshot
        # The panel imports it lazily: a real module already imported is a
        # package attribute and wins over sys.modules.
        for stub in (patch.dict(sys.modules, {'vpn2_emergency.snapshot': module}),
                     patch.object(sys.modules['vpn2_emergency'], 'snapshot', module, create=True)):
            stub.start()
            self.addCleanup(stub.stop)

        def make_server(state_dir, *, exit_id, public_ip, address, **providers):
            del address
            return FakeServer(web.Panel(state_dir, exit_id, public_ip, **providers))

        agent = cluster.agents[node]
        agent.snapshot_loader = module.load
        agent.lifecycle = lifecycle.Lifecycle(
            agent, hy2_module=FakeHy2(), web_module=types.SimpleNamespace(make_server=make_server),
            threaded=False, api_base=f'http://127.0.0.1:{bot.server_port}',
            probe_ready=lambda: True, probe_entry=lambda _host: True)
        agent.leader.events = agent.lifecycle
        return agent, bot

    def queued(self, agent):
        return [row for audience in ('admin', 'client')
                for row in tgqueue.delivery_status(agent.state.directory, audience)
                if row['state'] in ('pending', 'retry')]

    def test_isolated_leader_back_with_telegram_sends_nothing_before_stepping_down(self):
        cluster = self.cluster()
        agent, bot = self.announcer(cluster, 'c')
        online = agent.lifecycle.api_base
        cluster.stop('entry')
        cluster.advance(1320)
        self.assertEqual(cluster.leaders(), ['a'])
        # 'c' loses the other exits and Telegram alike.
        for node in ('a', 'b'):
            cluster.blocked.update({('c', node), (node, 'c')})
        agent.lifecycle.api_base = 'http://127.0.0.1:9'
        cluster.advance(390)
        self.assertEqual(set(cluster.leaders()), {'a', 'c'})
        self.assertTrue(agent.leader.data['autonomous'])
        self.assertIn('leader_changed', {row['event'] for row in self.queued(agent)})
        # Connectivity returns just as the queued notices are due again.
        while min(row['due'] for row in self.queued(agent)) > cluster.wall() + 15:
            cluster.advance(15)
        cluster.blocked.clear()
        agent.lifecycle.api_base = online
        with self.assertLogs(level='INFO') as logs:
            cluster.advance(45)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertIn('INFO:root:role=follower autonomous=false', logs.output)
        self.assertEqual(bot.texts, [])
        self.assertEqual(self.queued(agent), [])

    def test_isolated_leader_with_telegram_still_announces(self):
        cluster = self.cluster()
        agent, bot = self.announcer(cluster, 'c')
        cluster.stop('entry')
        cluster.advance(1320)
        for node in ('a', 'b'):
            cluster.blocked.update({('c', node), (node, 'c')})
        cluster.advance(390)
        self.assertTrue(agent.leader.data['autonomous'])
        cluster.advance(45)
        self.assertTrue(any('сменился' in text for _chat, text in bot.texts))

    def test_boot_change_expires_evidence_and_quarantines_votes(self):
        state = State(self.root, clock_boot='old')
        state.data['last_entry'] = 1000
        state.data['direct'] = dict(at=1000, epoch=1, run='old', sequence=1)
        state.save()
        fresh = State(self.root, clock_boot='new')
        hb = Heartbeat(fresh, 10)
        self.assertIsNone(hb.direct_evidence(10))
        leader = Leader(fresh, 'a', [{'id': 'a', 'priority': 0}], 'fixture', 10)
        leader.grant('a', 10, True)
        self.assertIsNone(leader.data['grant'])
        leader.grant('a', 10 + LEASE, True)
        self.assertEqual(leader.data['grant'], 'a')

    def test_state_private_bounded_and_exclusive_agent(self):
        cluster = self.cluster()
        agent = cluster.agents['a']
        self.assertEqual(agent.state.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(agent.state.directory.stat().st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(ValueError, 'already running'):
            Agent(cluster.configs['a'], self.root / 'a')
        agent.state.data['oversize'] = 'x' * MAX_STATE_BYTES
        with self.assertRaises(ValueError):
            agent.state.save()
        del agent.state.data['oversize']
        self.assertEqual(GUARD, 300)

    def outage(self, cluster):
        """Entry gone long enough for 'a' to lead an emergency."""
        cluster.stop('entry')
        cluster.advance(1320)
        self.assertEqual(cluster.leaders(), ['a'])
        return cluster.agents['a'].leader

    def entry_back(self, cluster, ready=True, port=None):
        if port is not None:
            cluster.configs['entry'] = dict(cluster.configs['entry'], nodes=dict(
                cluster.configs['entry']['nodes'], entry={'host': '127.0.0.1', 'port': port}))
        cluster.start('entry')
        cluster.agents['entry'].ready = ready
        cluster.drain()

    def recovered(self, cluster):
        leader = self.outage(cluster)
        self.entry_back(cluster)
        cluster.advance(150)
        recovery = leader.data.get('recovery')
        self.assertIsNotNone(recovery)
        return leader, recovery

    def test_entry_back_without_vpn_services_keeps_emergency(self):
        cluster = self.cluster()
        leader = self.outage(cluster)
        self.entry_back(cluster, ready=False)
        cluster.advance(1800)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertTrue(leader.data['active'])
        self.assertIsNone(leader.data.get('recovery'))
        self.assertFalse(any(call[0] == 'end' for event in cluster.events.values() for call in event.calls))

    def test_entry_must_stay_ready_two_minutes_before_recovery(self):
        cluster = self.cluster()
        leader = self.outage(cluster)
        self.entry_back(cluster)
        cluster.advance(135)
        self.assertIsNone(leader.data.get('recovery'))
        cluster.advance(15)
        self.assertEqual(leader.data['recovery']['round'], 1)
        self.assertIsNone(leader.data['recovery']['deadline'])
        # Recovery issues nothing: no new generation, no leader event.
        self.assertEqual(sum(call[0] == 'leader' for call in cluster.events['a'].calls), 1)

    def test_recovery_confirmed_through_real_lifecycle_probes(self):
        # Readiness comes from the lifecycle's probes, not from test flags:
        # a probe result must reach the round that reads it while still fresh.
        from vpn2_emergency.lifecycle import Lifecycle

        class Probes(Lifecycle):
            def _recovered(self, *_args):
                return None

            def _deliver(self, *_args):
                return None

        def attach(node):
            agent = cluster.agents[node]
            agent.lifecycle = Probes(agent, threaded=False, probe_ready=lambda: True,
                                     probe_entry=lambda _host: True)

        cluster = self.cluster()
        for node in 'abc':
            attach(node)
        leader = self.outage(cluster)
        cluster.start('entry')
        attach('entry')
        cluster.drain()
        cluster.advance(180)
        self.assertIsNotNone(leader.data.get('recovery'))

    def test_deadline_ends_emergency_on_every_exit_and_agent_keeps_running(self):
        cluster = self.cluster()
        leader, recovery = self.recovered(cluster)
        deadline = cluster.wall() + 600
        leader.set_deadline(recovery['round'], deadline, cluster.clock())
        self.assertEqual(cluster.events['a'].calls[-1][0], 'recovered')
        self.assertAlmostEqual(cluster.events['a'].calls[-1][1][0], cluster.clock() + 600)
        cluster.advance(30)
        self.assertEqual(cluster.agents['b'].leader.data['recovery']['deadline'], deadline)
        cluster.advance(540)
        self.assertEqual(cluster.leaders(), ['a'])
        cluster.advance(45)
        for node in 'abc':
            data = cluster.agents[node].leader.data
            self.assertFalse(data['active'], node)
            self.assertEqual(data['role'], 'observer', node)
            self.assertEqual(sum(call[0] == 'end' for call in cluster.events[node].calls), 1, node)
        cluster.advance(600)
        self.assertEqual(cluster.leaders(), [])
        self.assertTrue(all(agent.sock.fileno() >= 0 for agent in cluster.agents.values()))

    def test_late_delivery_does_not_extend_the_fixed_deadline(self):
        cluster = self.cluster()
        leader, recovery = self.recovered(cluster)
        deadline = cluster.wall() + 600
        leader.set_deadline(recovery['round'], deadline, cluster.clock())
        cluster.advance(300)
        leader.set_deadline(recovery['round'], cluster.wall() + 600, cluster.clock())
        self.assertEqual(leader.data['recovery']['deadline'], deadline)

    def test_entry_not_ready_again_cancels_return_and_keeps_access(self):
        cluster = self.cluster()
        leader, recovery = self.recovered(cluster)
        leader.set_deadline(recovery['round'], cluster.wall() + 600, cluster.clock())
        cluster.advance(420)
        cluster.agents['entry'].ready = False
        cluster.advance(45)
        self.assertIsNone(leader.data['recovery'])
        self.assertEqual(leader.data['cancelled_round'], recovery['round'])
        cluster.advance(600)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertTrue(all(agent.leader.data['active'] for node, agent in cluster.agents.items()
                            if node != 'entry'))
        self.assertFalse(any(call[0] == 'end' for event in cluster.events.values() for call in event.calls))
        # The next stable return is a new round with its own notice.
        cluster.agents['entry'].ready = True
        cluster.advance(150)
        self.assertEqual(leader.data['recovery']['round'], recovery['round'] + 1)

    def test_entry_gone_again_cancels_return(self):
        cluster = self.cluster()
        leader, recovery = self.recovered(cluster)
        cluster.stop('entry')
        cluster.advance(90)
        self.assertIsNone(leader.data['recovery'])
        self.assertEqual(cluster.leaders(), ['a'])

    def test_leader_restart_and_new_leader_keep_the_deadline(self):
        cluster = self.cluster()
        leader, recovery = self.recovered(cluster)
        deadline = cluster.wall() + 600
        leader.set_deadline(recovery['round'], deadline, cluster.clock())
        cluster.advance(30)
        cluster.stop('a')
        cluster.start('a')
        cluster.advance(30)
        self.assertEqual(cluster.agents['a'].leader.data['recovery']['deadline'], deadline)
        self.assertEqual(cluster.leaders(), ['a'])
        calls = len(cluster.events['b'].calls)
        cluster.stop('a')
        cluster.advance(90)
        self.assertEqual(cluster.leaders(), ['b'])
        self.assertEqual(cluster.agents['b'].leader.data['recovery']['deadline'], deadline)
        # No new bundle during the transition.
        self.assertFalse(any(call[0] == 'leader' for call in cluster.events['b'].calls[calls:]))
        while cluster.wall() < deadline + 30:
            cluster.advance()
        self.assertFalse(cluster.agents['b'].leader.data['active'])
        self.assertIn(('end', ()), cluster.events['b'].calls)

    def issuing(self, cluster, node, service, telegram=None):
        """Real lifecycle on an exit: Hysteria2, panel and Telegram are stubs."""
        agent = cluster.agents[node]
        panel = types.SimpleNamespace(
            url='https://127.0.0.1/', on_emergency_leader=lambda *_args: {'exit_name': node},
            on_step_down=lambda *_args: None, on_entry_recovered=lambda *_args: None,
            on_emergency_end=lambda: None, on_return_cancelled=lambda: None)
        server = types.SimpleNamespace(panel=panel, serve_forever=lambda: None,
                                       shutdown=lambda: None, server_close=lambda: None)
        quiet = lambda *_args, **_kwargs: None  # noqa: E731
        cancelled = service.cancelled = []
        telegram = types.SimpleNamespace(prune=quiet, enqueue_admin=quiet, enqueue_clients=quiet,
                                         cancel=lambda *_a, **kwargs: cancelled.append(kwargs.get('events')),
                                         run_once=quiet, delivery_status=lambda *_a: [],
                                         reachable=lambda *_a, **_k: True) if telegram is None else telegram
        agent.lifecycle = lifecycle.Lifecycle(
            agent, hy2_module=service, web_module=types.SimpleNamespace(make_server=lambda *_a, **_k: server),
            tg=telegram, threaded=False, probe_entry=lambda _host: True)
        events = cluster.events[node]
        for name in ('on_emergency_leader', 'on_step_down', 'on_entry_recovered', 'on_emergency_end'):
            setattr(events, name, (lambda name, record: lambda *args: (
                record(*args), getattr(agent.lifecycle, name)(*args)))(name, getattr(Events, name).__get__(events)))
        if agent.leader:
            agent.leader.events = events

    def cancelled_after_leader_change(self, cluster, services):
        # VPN2-520: the old leader dies right after the deadline is fixed, the
        # new one inherits the return without a bundle, the return is cancelled.
        for node in 'abc':
            self.issuing(cluster, node, services[node])
        leader, _recovery = self.recovered(cluster)
        cluster.advance(15)
        deadline = leader.data['recovery']['deadline']
        self.assertIsNotNone(deadline)
        self.assertTrue(services['a'].running)
        # A follower keeps `ready` from an earlier leadership of its own; it
        # must not make the old leader retire a bundle nobody replaced.
        cluster.agents['b'].leader.data.update(ready=True, ready_at=cluster.clock() - 3600)
        cluster.stop('a')
        cluster.advance(90)
        self.assertEqual(cluster.leaders(), ['b'])
        cluster.start('a')
        self.issuing(cluster, 'a', services['a'])
        cluster.advance(60)
        self.assertEqual(cluster.leaders(), ['b'])
        # Until the new leader has its own bundle the old one keeps serving.
        self.assertTrue(services['a'].running)
        cluster.agents['entry'].ready = False
        cluster.advance(45)
        self.assertLess(cluster.wall(), deadline)
        b = cluster.agents['b'].leader
        self.assertIsNone(b.data['recovery'])
        # The cancel notice of the return still goes out with the new bundle.
        self.assertIn(['recovered'], services['b'].cancelled)
        cluster.advance(30)
        self.assertTrue(services['b'].running)
        self.assertTrue(b.data['ready'])
        self.assertEqual(services['b'].starts[-1], b.data['issued_generation'])
        self.assertTrue(any(services[node].running for node in 'abc'))
        return b

    def test_leader_change_in_transition_then_cancel_keeps_access(self):
        cluster = self.cluster()
        services = {node: FakeHy2() for node in 'abc'}
        self.cancelled_after_leader_change(cluster, services)
        # The old bundle ends one transition after the new one is ready.
        cluster.advance(TRANSITION + 30)
        self.assertFalse(services['a'].running)
        self.assertTrue(services['b'].running)
        self.assertTrue(all(agent.leader.data['active'] for node, agent in cluster.agents.items()
                            if node != 'entry'))

    def test_leader_dies_right_after_return_notice_new_leader_keeps_its_deadline(self):
        # VPN2-521: the notice is out, the leader dies before the next round.
        cluster = self.cluster()
        services = {node: FakeHy2() for node in 'abc'}
        telegrams = {node: FakeTelegram() for node in 'abc'}
        for node in 'abc':
            self.issuing(cluster, node, services[node], telegrams[node])
        self.recovered(cluster)
        for _ in range(12):
            if telegrams['a'].sent:
                break
            cluster.advance(15)
        self.assertIn('admin', {audience for audience, _deadline in telegrams['a'].sent})
        self.assertEqual(len({deadline for _audience, deadline in telegrams['a'].sent}), 1)
        announced = telegrams['a'].sent[0][1]
        held = {node: cluster.agents[node].leader.data['recovery'].get('proposed') for node in 'bc'}
        cluster.stop('a')
        cluster.advance(120)
        self.assertEqual(cluster.leaders(), ['b'])
        b = cluster.agents['b'].leader
        self.assertEqual(lifecycle.iso(b.data['recovery']['deadline']), announced)
        self.assertEqual(telegrams['b'].sent, [])
        self.assertFalse([row for row in telegrams['b'].rows if row['event'] == 'recovered'])
        # The followers already held the announced time when it went out.
        self.assertEqual({lifecycle.iso(value) for value in held.values()}, {announced})
        while lifecycle.iso(cluster.wall()) < announced:
            cluster.advance()
        cluster.advance(30)
        self.assertFalse(b.data['active'])
        self.assertEqual(telegrams['b'].sent, [])

    def test_return_after_cancel_under_new_leader_ends_emergency(self):
        # The new leader inherited round 1 and cancelled it; the next return
        # must be a new round, or its notice and deadline never happen.
        cluster = self.cluster()
        services = {node: FakeHy2() for node in 'abc'}
        b = self.cancelled_after_leader_change(cluster, services)
        cancelled = b.data['cancelled_round']
        cluster.agents['entry'].ready = True
        cluster.advance(165)
        recovery = b.data['recovery']
        self.assertIsNotNone(recovery)
        self.assertGreater(recovery['round'], cancelled)
        self.assertIsNotNone(recovery['deadline'])
        cluster.advance(int(recovery['deadline'] - cluster.wall()) + 60)
        self.assertFalse(any(agent.leader.data['active'] for node, agent in cluster.agents.items()
                             if node != 'entry'))
        self.assertFalse(any(service.running for service in services.values()))

    def test_undelivered_notice_ends_after_two_hours(self):
        cluster = self.cluster()
        leader, _recovery = self.recovered(cluster)
        cluster.advance(7140)
        self.assertTrue(leader.data['active'])
        with self.assertLogs(level='INFO') as logs:
            cluster.advance(60)
        self.assertFalse(leader.data['active'])
        self.assertTrue(any('2' in line and 'emergency ended' in line for line in logs.output))

    def test_entry_on_new_address_is_recognised_by_key(self):
        cluster = self.cluster()
        self.outage(cluster)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertLogs(level='INFO') as logs:
            self.entry_back(cluster, port=port)
            cluster.advance(30)
        for node in 'abc':
            agent = cluster.agents[node]
            self.assertEqual(agent.addresses['entry'], ('127.0.0.1', port), node)
            self.assertEqual(agent.state.data['entry_moved']['to'], ['127.0.0.1', port])
        self.assertTrue(any('entry address changed' in line for line in logs.output))
        cluster.advance(150)
        self.assertIsNotNone(cluster.agents['a'].leader.data['recovery'])
        # A restarted exit keeps the learned address.
        cluster.stop('b')
        cluster.start('b')
        self.assertEqual(cluster.agents['b'].addresses['entry'], ('127.0.0.1', port))

    def test_forged_hello_from_another_address_does_not_move_entry(self):
        cluster = self.cluster()
        agent = cluster.agents['a']
        before = agent.addresses['entry']
        stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        stranger.bind(('127.0.0.1', 0))
        stranger.settimeout(2)
        self.addCleanup(stranger.close)
        hello = dict(sender='entry', installation_id='fixture', kind='hello', nonce='a' * 48)
        agent.receive(encode(b'x' * 32, hello), stranger.getsockname(), cluster.clock())
        agent.receive(encode(agent.key, hello), stranger.getsockname(), cluster.clock())
        challenge = decode(agent.key, stranger.recv(16385))
        # Without the matching nonce the reply is ignored.
        reply = dict(sender='entry', installation_id='fixture', kind='reply', nonce='b' * 48,
                     evidence=dict(epoch=1, run='x', sequence=1, age=0))
        agent.receive(encode(agent.key, reply), stranger.getsockname(), cluster.clock())
        self.assertEqual(agent.addresses['entry'], before)
        self.assertEqual(challenge['kind'], 'poll')

    def test_hooks_contract(self):
        calls = []
        for event in ('on_emergency_leader', 'on_step_down', 'on_entry_recovered', 'on_emergency_end'):
            hooks.subscribe(event, lambda *args: calls.append(args))
        try:
            hooks.on_emergency_leader('e', 1, False)
            hooks.on_step_down('b')
            hooks.on_entry_recovered(900)
            hooks.on_emergency_end()
            self.assertEqual(calls, [('e', 1, False), ('b',), (900,), ()])
            with self.assertRaises(ValueError):
                hooks.subscribe('invalid', calls.append)
        finally:
            hooks._listeners.clear()

    def test_provision_is_stable_private_and_separate_from_client_secrets(self):
        spec = importlib.util.spec_from_file_location('provision', ROOT / 'infra/emergency/provision.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (self.root / 'config.yaml').write_text(
            'entry: {host: 192.0.2.1, password: excluded}\n'
            'exits: [{tag: a, host: 192.0.2.2, password: excluded}, '
            '{tag: entry, host: 192.0.2.3, password: excluded}]\n')
        module.provision(self.root)
        path = self.root / 'infra/secrets/emergency/a.json'
        before = path.read_bytes()
        module.provision(self.root)
        self.assertEqual(before, path.read_bytes())
        self.assertNotIn('excluded', before.decode())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(before)['node'], 'a')
        entry = json.loads((path.parent / 'ru.json').read_text())
        self.assertEqual(entry['node'], entry['entry_id'])
        exit_config = json.loads((path.parent / 'entry.json').read_text())
        self.assertNotEqual(exit_config['node'], exit_config['entry_id'])

    def test_deploy_declares_agent_for_all_nodes(self):
        stage = (ROOT / 'infra/scripts/23-emergency.sh').read_text()
        deploy = (ROOT / 'infra/deploy.sh').read_text()
        self.assertIn('run_stage 23-emergency     all', deploy)
        self.assertIn('emergency/provision.py', deploy)
        self.assertIn('User=vpn2-emergency', stage)
        self.assertIn('LogRateLimitBurst=10', stage)
        self.assertIn('AmbientCapabilities=CAP_NET_BIND_SERVICE', stage)
        self.assertIn("ufw allow 443/tcp comment 'vpn2 emergency panel'", stage)
        self.assertIn("print('any', port)", stage)
        self.assertIn('systemctl is-active --quiet vpn2-emergency.service', stage)
        result = subprocess.run(['bash', '-n', str(ROOT / 'infra/scripts/23-emergency.sh')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
