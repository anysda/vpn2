"""Single-process UDP observer; no external packages on entry or exits."""

import argparse
import fcntl
import importlib
import json
import logging
from pathlib import Path
import secrets
import selectors
import signal
import socket
import time
import uuid

from .heartbeat import Heartbeat, MAX_PACKET, MISSED, PERIOD, decode, encode
from .leader import Leader
from .lifecycle import Lifecycle
from .state import State


def load_snapshot(state_dir):
    try:
        module = importlib.import_module('.snapshot', __package__)
    except ModuleNotFoundError as error:
        if error.name != __package__ + '.snapshot':
            raise
        return None
    return module.load(state_dir)


class Agent:
    def __init__(self, config, state_dir, clock=time.monotonic, snapshot_loader=load_snapshot,
                 wall=time.time):
        self.config = config
        self.clock = clock
        self.wall = wall
        self.snapshot_loader = snapshot_loader
        self.state = State(state_dir)
        self.lock = (self.state.directory / 'agent.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError('observer already running') from None
        self.node = config['node']
        self.entry = config.get('entry_id', 'entry')
        self.installation = config['installation_id']
        self.key = bytes.fromhex(config['key'])
        if len(self.key) != 32 or len(config['nodes']) > 65:
            raise ValueError('invalid observer configuration')
        self.addresses = {node: (socket.gethostbyname(item['host']), item['port'])
                          for node, item in config['nodes'].items()}
        # An entry reinstalled on another address is recognised by its key and
        # kept until the configuration itself names a different address.
        moved = self.state.data.get('entry_moved')
        if moved and self.node != self.entry and tuple(moved['from']) == self.addresses.get(self.entry):
            self.addresses[self.entry] = tuple(moved['to'])
        self.moving = None
        # Entry: own VPN readiness, set by the lifecycle probe. Exit: the
        # entry's last reported readiness and the exit's own reachability check.
        self.ready = False
        self.entry_reply = None
        self.entry_check = None
        self.lifecycle = None
        self.heartbeat = Heartbeat(self.state, clock())
        self.leader = None
        self.last_log = None
        self.stop_requested = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((config.get('bind', '0.0.0.0'), config['nodes'][self.node]['port']))
        self.sock.setblocking(False)
        if self.node == self.entry:
            self.state.data['epoch'] = self.state.data.get('epoch', 0) + 1
            self.state.data['run'] = str(uuid.uuid4())
            self.state.data['sequence'] = 0
        self.state.save()

    def close(self):
        self.sock.close()
        self.lock.close()

    def send(self, peer, message, address=None):
        message = message | {'installation_id': self.installation, 'sender': self.node}
        try:
            self.sock.sendto(encode(self.key, message), address or self.addresses[peer])
        except OSError:
            # A missing route is an unknown peer, never proof of entry death.
            pass

    def status(self, now):
        return self.leader.status(now) if self.leader else None

    def tick(self, now):
        if self.lifecycle:
            # A check stamped one round ago is read now; applied after the
            # round it would be two rounds old and never count as fresh.
            self.lifecycle.collect(now)
        if self.node == self.entry:
            self.state.data['sequence'] += 1
            # Exits that still poll an old address learn the new one from this.
            for peer in self.addresses:
                if peer != self.node:
                    self.send(peer, {'kind': 'hello', 'nonce': secrets.token_hex(24)})
        else:
            snapshot = self.snapshot_loader(self.state.directory)
            if snapshot is not None:
                if snapshot['installation_id'] != self.installation:
                    raise ValueError('snapshot belongs to another installation')
                exits = snapshot['exits']
                if self.leader is None:
                    self.leader = Leader(self.state, self.node, exits, self.installation, now, wall=self.wall)
                elif {item['id']: (item['priority'], item['id']) for item in exits} != self.leader.ranks:
                    # Membership changes require a full lease without voting.
                    # Wave integration can explicitly restart after snapshot update.
                    raise ValueError('election membership changed; restart required')
            if self.leader:
                self.leader.tick(now, self.heartbeat.emergency_due(now))
                self.leader.entry_state(now, self.entry_ready(now))
            for peer in self.addresses:
                if peer == self.node:
                    continue
                if peer != self.entry and not (self.heartbeat.poll_due(now) or
                                           self.leader and self.leader.data['active']):
                    continue
                self.send(peer, {'kind': 'poll', 'nonce': self.heartbeat.challenge(peer, now),
                                 'candidate': self.leader.candidate if self.leader else None})
        self.state.save()
        self.log_role()
        if self.lifecycle:
            self.lifecycle.tick(now)

    def entry_ready(self, now):
        """Fresh entry answer: its VPN services and control traffic, plus our own check."""
        if not self.entry_reply or now - self.entry_reply[1] >= 2 * PERIOD:
            return None
        check = self.entry_check
        reachable = check is None or (now - check[1] < 2 * PERIOD and check[0])
        return bool(self.entry_reply[0] and reachable)

    def relocate(self, peer, message, address, now):
        """Only the entry may come back elsewhere: it must answer our fresh challenge there."""
        if peer != self.entry or self.node == self.entry:
            return False
        if message.get('kind') == 'hello':
            nonce = self.heartbeat.challenge(peer, now)
            self.moving = (address, nonce)
            self.send(peer, {'kind': 'poll', 'nonce': nonce,
                             'candidate': self.leader.candidate if self.leader else None}, address)
            return False
        if message.get('kind') != 'reply' or self.moving != (address, message.get('nonce')):
            return False
        old = self.addresses[peer]
        self.addresses[peer] = address
        self.moving = None
        configured = self.config['nodes'][peer]
        self.state.data['entry_moved'] = {
            'from': [socket.gethostbyname(configured['host']), configured['port']], 'to': list(address),
            'emergency': self.leader.data['emergency_id'] if self.leader else ''}
        self.state.save()
        logging.info('entry address changed: %s -> %s', old[0], address[0])
        return True

    def log_role(self):
        role = self.leader.data['role'] if self.leader else 'observer'
        autonomous = bool(self.leader and self.leader.data.get('autonomous'))
        summary = (role, autonomous)
        if summary != self.last_log:
            logging.info('role=%s autonomous=%s', role, str(autonomous).lower())
            self.last_log = summary

    def receive(self, packet, address, now):
        try:
            message = decode(self.key, packet)
            peer = message.get('sender')
            if (message.get('installation_id') != self.installation or peer == self.node
                    or peer not in self.addresses):
                return
            if address != self.addresses[peer] and not self.relocate(peer, message, address, now):
                return
            nonce = message.get('nonce')
            if not isinstance(nonce, str) or len(nonce) != 48:
                return
            if message.get('kind') == 'poll':
                if self.node == self.entry:
                    evidence = {k: self.state.data[k] for k in ('epoch', 'run', 'sequence')}
                    evidence['age'] = 0
                else:
                    if peer == self.entry:
                        return
                    if self.leader and message.get('candidate') == peer:
                        self.leader.grant(message.get('candidate'), now,
                                          self.heartbeat.emergency_due(now) or self.leader.data['active'])
                    evidence = self.heartbeat.direct_evidence(now)
                reply = {'kind': 'reply', 'nonce': nonce, 'evidence': evidence, 'status': self.status(now)}
                if self.node == self.entry:
                    reply['ready'] = self.ready is True
                self.send(peer, reply)
            elif message.get('kind') == 'reply' and self.node != self.entry:
                transit = self.heartbeat.response(peer, nonce, now)
                if transit is None:
                    return
                self.heartbeat.receive(message.get('evidence'), now, transit,
                                       direct=peer == self.entry, observer=peer)
                if peer == self.entry:
                    self.entry_reply = (message.get('ready') is True, now)
                if self.leader and peer != self.entry:
                    self.leader.observe(peer, message.get('status'), now, transit,
                                        allow_activation=now - self.heartbeat.last_entry >= MISSED)
                    # Act on the received lease immediately. Waiting for the
                    # next periodic poll adds another 15 s to a staggered
                    # election and can exceed the 90 s failover budget.
                    self.leader.tick(now, self.heartbeat.emergency_due(now))
                self.state.save()
                # The role changes here, between rounds; logging it only on
                # the next round put the journal up to 15 s behind the fact.
                self.log_role()
        except (ValueError, TypeError, KeyError, OverflowError):
            # Invalid unauthenticated traffic must neither fill logs nor stop
            # observation. Local state/IO errors intentionally fail the service.
            return

    def run(self):
        next_tick = self.clock()
        with selectors.DefaultSelector() as selector:
            selector.register(self.sock, selectors.EVENT_READ)
            while not self.stop_requested:
                now = self.clock()
                if now >= next_tick:
                    self.tick(now)
                    next_tick = now + PERIOD
                for _key, _mask in selector.select(min(1, max(0, next_tick - self.clock()))):
                    # Bounded work per iteration even when the port is flooded.
                    for _ in range(64):
                        try:
                            packet, address = self.sock.recvfrom(MAX_PACKET + 1)
                        except BlockingIOError:
                            break
                        self.receive(packet, address, self.clock())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--state-dir', default='/var/lib/vpn2-emergency')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    agent = Agent(json.loads(Path(args.config).read_text()), args.state_dir)
    agent.lifecycle = Lifecycle(agent)
    agent.lifecycle.attach()

    def stop(_signum, _frame):
        agent.stop_requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        agent.run()
    finally:
        agent.lifecycle.close()
        agent.close()


if __name__ == '__main__':
    main()
