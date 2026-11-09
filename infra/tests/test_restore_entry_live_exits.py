"""Entry reinstalled from backup while the exits keep running (docs/backup.md).

A new entry brings a new emergency identity: installation and key. The exits
still hold the previous installation's snapshot, entry cursor and, after a
long outage, a running emergency. Real agents, real snapshot files, real
receiver; the same assertions can target an older tree using VPN2_TEST_ROOT.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'infra/emergency'))

from vpn2_emergency import lifecycle, snapshot, snapshot_sender  # noqa: E402
from vpn2_emergency.agent import Agent  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    'restore_agent_harness', Path(__file__).resolve().parent / 'test_emergency_agent.py')
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

# The exit applies expiry with its own clock: scenarios run in the recent past.
NOW = int(time.time()) - 10_000
NOTHING = {'clients': [], 'bot_token': None, 'admin_chat_id': None}


class Stream:
    def __init__(self, raw):
        self.raw = raw

    def read(self, _size):
        return self.raw


class Installation(harness.Cluster):
    """Entry and exits reading the snapshot the receiver really wrote."""

    def __init__(self, directory, count=2):
        self.hy2 = {}
        self.telegram = {}
        super().__init__(directory, count)

    def snapshot(self, directory):
        return snapshot.load(directory)

    def image(self, installation, version=1):
        return {'schema': 1, 'version': version, 'installation_id': installation,
                'created_at': snapshot.iso_time(NOW), 'admin_chat_id': None, 'bot_token': None,
                'exits': self.exits, 'clients': []}

    def start(self, node):
        if node == 'entry' or node not in self.hy2:
            return super().start(node)
        # An exit after stage 23: the agent comes up with its lifecycle, the
        # Hysteria2 unit and the notice queue survive the restart.
        agent = Agent(self.configs[node], self.directory / node, clock=self.clock,
                      snapshot_loader=self.snapshot, **self.wall_clock())
        self.agents[node] = agent
        agent.lifecycle = lifecycle.Lifecycle(
            agent, hy2_module=self.hy2[node], web_module=types.SimpleNamespace(make_server=None),
            tg=self.telegram[node], threaded=False, probe_ready=lambda: True,
            probe_entry=lambda _host: True)
        agent.tick(self.clock())
        if agent.leader:
            agent.leader.events = self.events.setdefault(node, harness.Events())
        return None

    def new_entry(self, installation, key):
        """Fresh entry host; deploy renders a new identity for every node."""
        if 'entry' in self.agents:
            self.stop('entry')
        shutil.rmtree(self.directory / 'entry')
        for node in self.configs:
            self.configs[node] = dict(self.configs[node], installation_id=installation, key=key)
        self.start('entry')

    def restart_exits(self):
        """Stage 23 on the exits: new agent.json, agent restarted."""
        for item in self.exits:
            node = item['id']
            self.hy2.setdefault(node, harness.FakeHy2())
            self.telegram.setdefault(node, harness.FakeTelegram())
            self.stop(node)
            self.start(node)
        self.drain()


class Exits:
    """Receivers as SSH runs them; each reads the installation from its agent.json."""

    def __init__(self, cluster):
        self.cluster = cluster
        self.installation = {item['id']: 'fixture' for item in cluster.exits}

    def deliver(self, exit_id, request):
        stream = Stream(snapshot.encode(request))
        return snapshot.receive(stream, self.cluster.directory / exit_id,
                                self.installation[exit_id], NOW)


class TestRestoreEntryWithLiveExits(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cluster = Installation(self.root / 'nodes')
        self.addCleanup(self.cluster.close)
        for item in self.cluster.exits:
            result = snapshot.accept(self.root / 'nodes' / item['id'],
                                     self.cluster.image('fixture'), None, 'fixture', NOW)
            self.assertEqual(result['status'], 'accepted')
        self.cluster.advance(30)

    def send(self, sender, exits, now):
        config = {'state_dir': sender, 'agent': self.cluster.configs['entry']}
        return snapshot_sender.run_once(config, exits.deliver, lambda _moment: NOTHING, now=now)

    def assert_installed(self, installation):
        """One installation on every node, a fresh image, election and entry heard."""
        for item in self.cluster.exits:
            agent = self.cluster.agents[item['id']]
            self.assertEqual(agent.installation, installation)
            self.assertEqual(snapshot.load(agent.state.directory)['installation_id'], installation)
            self.assertIsNotNone(agent.leader)
            self.assertFalse(agent.leader.data['active'])
            self.assertFalse(self.cluster.hy2[item['id']].running)
        self.assertEqual(self.cluster.agents['entry'].installation, installation)
        # The new entry stays alive past the outage threshold: no new emergency.
        self.cluster.advance(1500)
        self.assertEqual(self.cluster.leaders(), [])
        for item in self.cluster.exits:
            agent = self.cluster.agents[item['id']]
            self.assertFalse(agent.leader.data['active'])
            self.assertLess(self.cluster.clock() - agent.heartbeat.last_entry, 60)

    def restore(self, exits, sender):
        """deploy.sh on the new entry: stage 23 on the exits and on the entry in parallel."""
        self.cluster.new_entry('new', 'cd' * 32)
        # The entry's sender runs before the exits got their new agent.json.
        self.assertEqual(set(self.send(sender, exits, NOW).values()), {'wrong-installation'})
        self.cluster.restart_exits()
        for item in self.cluster.exits:
            exits.installation[item['id']] = 'new'
            agent = self.cluster.agents[item['id']]
            # The previous installation's image is gone, nothing elected on it.
            self.assertIsNone(snapshot.load(agent.state.directory))
            self.assertIsNone(agent.leader)
        # Next reconciliation round delivers the new image without any manual step.
        results = self.send(sender, exits, NOW + snapshot_sender.RECONCILE_SECONDS)
        self.assertEqual(set(results.values()), {'accepted'})
        self.cluster.advance(30)

    def test_exits_take_the_new_installation(self):
        exits, sender = Exits(self.cluster), self.root / 'sender'
        self.restore(exits, sender)
        self.assert_installed('new')

    def test_exit_in_emergency_leaves_it_by_itself(self):
        cluster = self.cluster
        cluster.stop('entry')
        cluster.advance(1320)
        self.assertEqual(cluster.leaders(), ['a'])
        emergency = cluster.agents['a'].leader.data['emergency_id']
        # Hysteria2 of the emergency is up on the leader and survives restarts.
        cluster.hy2['a'] = harness.FakeHy2()
        cluster.hy2['a'].running = True
        cluster.telegram['a'] = harness.FakeTelegram()
        cluster.telegram['a'].enqueue_clients(emergency_id=emergency)
        exits, sender = Exits(cluster), self.root / 'sender'
        self.restore(exits, sender)
        self.assertEqual(cluster.telegram['a'].rows[0]['state'], 'cancelled')
        self.assert_installed('new')

    def test_sender_asks_again_after_wrong_installation(self):
        exits, sender = Exits(self.cluster), self.root / 'sender'
        self.cluster.configs['entry'] = dict(self.cluster.configs['entry'], installation_id='new')
        self.assertEqual(set(self.send(sender, exits, NOW).values()), {'wrong-installation'})
        for item in self.cluster.exits:
            # The exit's agent restarted with the new identity, the old image is gone.
            exits.installation[item['id']] = 'new'
            for name in (snapshot.NAME, snapshot.CONFIRMED):
                (self.root / 'nodes' / item['id'] / name).unlink()
        self.assertEqual(set(self.send(sender, exits, NOW + 5).values()), {'wrong-installation'})
        results = self.send(sender, exits, NOW + snapshot_sender.RECONCILE_SECONDS)
        self.assertEqual(set(results.values()), {'accepted'})


VERIFY = ROOT / 'infra/scripts/99-verify.sh'


class TestStandbyBeforeStage23(unittest.TestCase):
    """99-verify right after 10-foreign meets an emergency the new entry ends."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'bin').mkdir()
        ss = self.root / 'bin/ss'
        ss.write_text('#!/bin/sh\necho "UNCONN 0 0 0.0.0.0:18443 0.0.0.0:*"\n')
        ss.chmod(0o755)
        (self.root / 'hy2.json').write_text(json.dumps({'server_ip': '192.0.2.2', 'port': 18443}))
        (self.root / 'state').mkdir()

    def standby(self, stage23_newer, active):
        source = VERIFY.read_text()
        start = source.index('emergency_standby() {')
        function = source[start:source.index('\n}\n', start) + 3]
        for old, new in (('/etc/vpn2/emergency-hy2.json', self.root / 'hy2.json'),
                         ('/var/lib/vpn2-emergency', self.root / 'state'),
                         ('/var/anysda/.stamps/10-foreign', self.root / 'stamp-10')):
            function = function.replace(old, str(new))
        (self.root / 'state/agent.json').write_text(json.dumps({'leader': {'active': active}}))
        stamps = [self.root / 'stamp-10', self.root / 'stamp-23']
        for index, path in enumerate(stamps if stage23_newer else stamps[::-1]):
            path.touch()
            os.utime(path, (NOW + index, NOW + index))
        script = f'EMERGENCY_STAMP={self.root / "stamp-23"}\n{function}emergency_standby\n'
        env = dict(os.environ, PATH=f"{self.root / 'bin'}:{os.environ['PATH']}")
        return subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)

    def test_listening_port_during_emergency_before_stage_23(self):
        self.assertEqual(self.standby(stage23_newer=False, active=True).returncode, 0)

    def test_listening_port_stays_a_failure_otherwise(self):
        for stage23_newer, active in ((True, True), (False, False), (True, False)):
            with self.subTest(stage23_newer=stage23_newer, active=active):
                result = self.standby(stage23_newer, active)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('udp/18443 уже слушает', result.stdout)


if __name__ == '__main__':
    unittest.main()
