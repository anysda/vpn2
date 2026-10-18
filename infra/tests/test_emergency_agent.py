"""Real UDP state machines driven by a monotonic test clock; no sleeping.

The same assertions can target an older tree using VPN2_TEST_ROOT.
"""

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'infra/emergency'))

from vpn2_emergency import hooks  # noqa: E402
from vpn2_emergency.agent import Agent  # noqa: E402
from vpn2_emergency.heartbeat import Heartbeat, decode, encode  # noqa: E402
from vpn2_emergency.leader import GUARD, LEASE, Leader  # noqa: E402
from vpn2_emergency.state import MAX_STATE_BYTES, State  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Events:
    def __init__(self):
        self.calls = []

    def on_emergency_leader(self, *args):
        self.calls.append(('leader', args))

    def on_step_down(self, *args):
        self.calls.append(('step_down', args))

    def on_emergency_end(self):
        self.calls.append(('end', ()))


class Cluster:
    def __init__(self, directory, count=3):
        self.directory = Path(directory)
        self.clock = Clock()
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

    def snapshot(self, _directory):
        return {'installation_id': 'fixture', 'exits': self.exits}

    def start(self, node):
        agent = Agent(self.configs[node], self.directory / node, clock=self.clock,
                      snapshot_loader=self.snapshot)
        original_send = agent.send

        def send(peer, message):
            if (node, peer) not in self.blocked:
                original_send(peer, message)

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
        self.assertEqual(len(cluster.events['b'].calls), 1)
        cluster.start('a')
        cluster.advance(90)
        self.assertEqual(cluster.leaders(), ['b'])

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
        self.assertIn('systemctl is-active --quiet vpn2-emergency.service', stage)
        result = subprocess.run(['bash', '-n', str(ROOT / 'infra/scripts/23-emergency.sh')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
