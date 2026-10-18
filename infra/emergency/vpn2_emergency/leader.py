"""Leased voting plus explicitly degraded minority operation.

Normal elections require a majority of the snapshot membership. A minority
can serve only after three failed rounds AND 300 seconds of isolation. This
is deliberately available under partition, not a claim of linearizability.
"""

import math
import uuid

from . import hooks
from .heartbeat import MISSED

LEASE = 75
GUARD = 300
TRANSITION = 600


class Leader:
    def __init__(self, state, node, exits, installation, now, events=hooks):
        self.state = state
        self.node = node
        self.installation = installation
        self.events = events
        self.ranks = {item['id']: (item['priority'], item['id']) for item in exits}
        if node not in self.ranks or len(self.ranks) > 64:
            raise ValueError('invalid election membership')
        self.data = state.data.setdefault('leader', {})
        self.peers = self.data.setdefault('peers', {}) if state.same_boot else {}
        self.data['peers'] = self.peers
        self.data.setdefault('role', 'observer')
        self.data.setdefault('generation', 0)
        self.data.setdefault('active', False)
        self.data.setdefault('owner', None)
        self.data.setdefault('emergency_id', '')
        self.data.setdefault('grant', None)
        self.data.setdefault('grant_until', 0)
        if not state.same_boot:
            # No monotonic lease survives a host reboot. Quarantine voting for
            # a full lease so a previously granted vote cannot be double-spent.
            self.data['grant_until'] = now + LEASE
            self.data['grant'] = None
            self.data['role'] = 'observer'
            self.data.pop('retire_at', None)
        self.candidate = None
        self.failed_since = None
        self.failed_rounds = 0
        self.last_round = -math.inf

    def status(self, now):
        return {key: self.data.get(key) for key in (
            'role', 'generation', 'active', 'owner', 'emergency_id', 'autonomous', 'ready')} | {
            'grant': self.data['grant'],
            'lease': max(0, self.data['grant_until'] - now)}

    def observe(self, node, status, now, transit=0, allow_activation=True):
        if node not in self.ranks or node == self.node or not isinstance(status, dict):
            return False
        if (status.get('role') not in ('observer', 'follower', 'leader')
                or type(status.get('generation')) is not int
                or not 0 <= status['generation'] < 2**53
                or type(status.get('lease')) not in (int, float)
                or not math.isfinite(status['lease']) or not 0 <= status['lease'] <= LEASE
                or status.get('grant') not in (*self.ranks, None)
                or status.get('owner') not in (*self.ranks, None)
                or not isinstance(status.get('emergency_id'), str)
                or len(status['emergency_id']) > 64):
            return False
        self.peers[node] = (dict(status, lease_until=now + max(0, status['lease'] - transit)), now)
        if allow_activation and status.get('active'):
            self.data['active'] = True
        self.data['generation'] = max(self.data['generation'], status['generation'])
        return True

    def preferred(self, now):
        leaders = {node: status for node, (status, at) in self.peers.items()
                   if now - at < LEASE and status['role'] == 'leader'}
        if self.data['role'] == 'leader':
            leaders[self.node] = self.data
        if leaders:
            # An existing confirmed leader beats an isolated autonomous one;
            # equal modes use a total order, never reciprocal redirects.
            return min(leaders, key=lambda node: (bool(leaders[node].get('autonomous')),
                                                  self.ranks[node]))
        reachable = [node for node, (_status, at) in self.peers.items() if now - at < MISSED]
        return min([self.node, *reachable], key=self.ranks.__getitem__)

    def grant(self, candidate, now, eligible):
        if not eligible or candidate not in self.ranks:
            return
        # A replayed signed poll cannot renew a dead candidate. Only a reply
        # to OUR fresh challenge establishes bidirectional contact, and the
        # grant never extends beyond that observation plus one lease.
        confirmed_at = now if candidate == self.node else self.peers.get(candidate, ({}, -math.inf))[1]
        if now - confirmed_at >= MISSED:
            return
        if self.data['grant_until'] > now and self.data['grant'] != candidate:
            return
        if self.preferred(now) != candidate:
            return
        self.data['grant'] = candidate
        self.data['grant_until'] = confirmed_at + LEASE
        self.state.save()

    def _step_down(self, winner):
        if self.data['role'] == 'leader':
            self.events.on_step_down(winner)
        self.data['role'] = 'follower'
        self.data['redirect'] = winner
        self.state.save()

    def _become(self, autonomous):
        changed = self.data['owner'] != self.node or self.data.get('credentials_lost', False)
        resumed = self.data['role'] != 'leader'
        self.data.update(role='leader', owner=self.node, redirect=None, autonomous=autonomous)
        if changed:
            self.data['generation'] += 1
            self.data['issued_generation'] = self.data['generation']
            self.data['credentials_lost'] = False
            self.data['ready'] = False
        if not self.data['emergency_id']:
            self.data['emergency_id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, self.installation + ':' +
                                                     str(self.state.data.get('entry_cursor', [0, '', 0])[:2])))
        if changed or resumed:
            self.data['pending_leader'] = True
        self.state.save()
        self.dispatch()

    def dispatch(self):
        # The durable event key allows subscribers to continue an existing
        # queue after a crash between side effect and acknowledgement.
        if self.data.get('pending_leader'):
            self.events.on_emergency_leader(self.data['emergency_id'],
                                            self.data['issued_generation'],
                                            self.data['autonomous'])
            self.data['pending_leader'] = False
            self.state.save()

    def bundle_ready(self, now):
        """Called by integration only after connection self-test and issuance."""
        self.data['ready'] = True
        self.data['ready_at'] = now
        self.state.save()

    def credentials_lost(self):
        self.data['credentials_lost'] = True
        self.state.save()

    def tick(self, now, outage):
        if self.data.get('retire_at') is not None and now >= self.data['retire_at']:
            self.events.on_emergency_end()
            del self.data['retire_at']
            self.data['retired'] = True
            self.state.save()
        if not (outage or self.data['active']):
            return
        self.data['active'] = True
        winner = self.preferred(now)
        self.candidate = winner
        if winner != self.node:
            status = self.peers[winner][0]
            if status['role'] == 'leader':
                previous_owner = self.data['owner']
                if self.data.get('redirect') != winner or self.data['role'] == 'leader':
                    self._step_down(winner)
                self.data['owner'] = winner
                self.data['emergency_id'] = status['emergency_id']
                if previous_owner == self.node:
                    self.data['retiring'] = True
                if self.data.get('retiring') and status.get('ready'):
                    self.data.setdefault('retire_at', now + TRANSITION)
                    self.data['retiring'] = False
                self.failed_rounds = 0
                self.failed_since = None
                self.state.save()
                return
            # A merely reachable higher-ranked candidate cannot suppress
            # autonomous operation forever under asymmetric connectivity.
        else:
            self.grant(self.node, now, True)
            votes = int(self.data['grant'] == self.node and self.data['grant_until'] > now)
            votes += sum(status['grant'] == self.node and status['lease_until'] > now
                         for status, at in self.peers.values() if now - at < LEASE)
            if len(self.ranks) > 1 and votes >= len(self.ranks) // 2 + 1:
                self.failed_since = None
                self.failed_rounds = 0
                self._become(False)
                return
        if self.failed_since is None:
            self.failed_since = now
        if now - self.last_round >= 15:
            self.failed_rounds += 1
            self.last_round = now
        if self.failed_rounds >= 3 and now - self.failed_since >= GUARD:
            self._become(True)
        elif self.data['role'] == 'leader':
            self._step_down(None)
        elif self.data.get('redirect'):
            self.data['redirect'] = None
            self.state.save()
