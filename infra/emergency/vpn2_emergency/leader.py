"""Leased voting plus explicitly degraded minority operation.

Normal elections require a majority of the snapshot membership. A minority
can serve only after three failed rounds AND 300 seconds of isolation. This
is deliberately available under partition, not a claim of linearizability.
"""

import logging
import math
import time
import uuid

from . import hooks
from .heartbeat import MISSED, PERIOD

# Failover budget is 90 s. A follower honours the dead leader for LEASE after
# its last reply and asks for votes only on its own PERIOD round, which can
# start just before that expiry: worst case (LEASE // PERIOD + 1) * PERIOD =
# 75 s, leaving one more round for a lost datagram. A working leader renews
# every vote each round, so a lease is never older than one round plus
# transit; 60 s still survives two lost rounds before MISSED refuses renewal.
LEASE = 60
GUARD = 300
TRANSITION = 600
# A leader keeps its role through a lost round: under 30 % packet loss one
# round without a majority is ordinary. It steps down after this many rounds
# in a row or once its last majority is that old, still inside one lease. A
# restarted leader whose majority is older steps down at once, as before.
STEP_DOWN_ROUNDS = (LEASE - PERIOD) // PERIOD
# The entry counts as back only after it reported working VPN services and the
# exit reached it for this long without a break. The first heartbeat after an
# outage is only a link and never ends an emergency.
STABLE = 120
# Emergency access ends this long after the first delivered recovery notice;
# a later delivery never extends it. Without any delivery it ends after
# TELEGRAM_WAIT, without a notice.
NOTICE = 600
TELEGRAM_WAIT = 7200
# The leader proposes the deadline first and announces it only once every exit
# that follows it echoes the proposal: a leader that dies right after the
# notice leaves its deadline with the others. The margin covers the echo (two
# rounds); a proposal closer than NOTICE is replaced before any delivery.
PROPOSAL_MARGIN = 2 * PERIOD
LOG = logging.getLogger(__name__)


def deadline_after(wall):
    """Whole minute, so the notice and the panel show the same time."""
    return math.ceil((wall + NOTICE) / 60) * 60


def valid_recovery(value):
    if value is None:
        return True
    optional = tuple(key for key in ('deadline', 'proposed') if value.get(key) is not None) \
        if isinstance(value, dict) else ()
    return (isinstance(value, dict) and type(value.get('round')) is int and value['round'] > 0
            and all(type(value.get(key)) in (int, float) and math.isfinite(value[key])
                    for key in ('confirmed', *optional)))


class Leader:
    def __init__(self, state, node, exits, installation, now, events=hooks, wall=time.time):
        self.state = state
        self.wall = wall
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
            self.data['ready'] = False
            self.data.pop('retire_at', None)
            self.data.pop('failed_since', None)
            self.data.pop('failed_rounds', None)
            self.data.pop('last_round', None)
            self.data.pop('majority_at', None)
        self.candidate = None
        # Entry readiness streaks are local; the recovery itself is agreed state.
        self.ready_since = None
        self.not_ready = 0
        self.stale_since = None
        # The deadline this process proposed; any other one was inherited.
        self.proposed = None
        self.data.setdefault('recovery', None)
        self.data.setdefault('failed_since', None)
        self.data.setdefault('failed_rounds', 0)
        self.data.setdefault('last_round', now - 15)

    def status(self, now):
        return {key: self.data.get(key) for key in (
            'role', 'generation', 'active', 'owner', 'emergency_id', 'autonomous', 'ready',
            'recovery', 'previous_emergency')} | {
            'grant': self.data['grant'],
            'lease': max(0, self.data['grant_until'] - now),
            'ready_age': max(0, now - self.data['ready_at']) if self.data.get('ready') else None}

    def observe(self, node, status, now, transit=0, allow_activation=True):
        if node not in self.ranks or node == self.node or not isinstance(status, dict):
            return False
        if (status.get('role') not in ('observer', 'follower', 'leader')
                or type(status.get('generation')) is not int
                or not 0 <= status['generation'] < 2**53
                or type(status.get('lease')) not in (int, float)
                or not math.isfinite(status['lease']) or status['lease'] < 0
                or status.get('grant') not in (*self.ranks, None)
                or status.get('owner') not in (*self.ranks, None)
                or not isinstance(status.get('emergency_id'), str)
                or len(status['emergency_id']) > 64
                or not isinstance(status.get('previous_emergency') or '', str)
                or not valid_recovery(status.get('recovery'))):
            return False
        if status.get('ready') and (type(status.get('ready_age')) not in (int, float)
                                    or not math.isfinite(status['ready_age']) or status['ready_age'] < 0):
            return False
        # A longer lease from a peer with older constants is cut, never trusted.
        lease = min(status['lease'], LEASE)
        self.peers[node] = (dict(status, lease_until=now + max(0, lease - transit),
                                ready_age=status['ready_age'] + transit if status.get('ready') else None), now)
        if (self.data['active'] and node == self.data['owner'] and self.data['emergency_id']
                and status.get('previous_emergency') == self.data['emergency_id']):
            # The owner already reached the deadline; a missed round must not
            # leave this exit in an emergency nobody serves.
            self.finish('завершено ведущим узлом')
            return True
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
        # Only this node counts its own vote, so a candidacy that is not a
        # leadership can be dropped at once in favour of a working leader or
        # a higher-ranked exit. Holding it for a full lease after a restart
        # left the leader without a majority once a third exit was cut off.
        released = self.data['grant'] == self.node and self.data['role'] != 'leader'
        if self.data['grant_until'] > now and self.data['grant'] != candidate and not released:
            return
        if self.preferred(now) != candidate:
            return
        self.data['grant'] = candidate
        self.data['grant_until'] = confirmed_at + LEASE
        self.state.save()

    def _step_down(self, winner):
        if self.data['role'] == 'leader' or self.data.get('redirect') != winner:
            self.events.on_step_down(winner)
        self.data['role'] = 'follower'
        self.data['redirect'] = winner
        # Autonomy describes only a leader; a follower logged autonomous=true.
        self.data['autonomous'] = False
        self.state.save()

    def _become(self, autonomous):
        if self.data.get('recovery'):
            # The return is confirmed: a new leader only keeps the agreed
            # deadline and ends the emergency, it never issues a new bundle.
            # Without its own bundle it is not ready, so the former leader
            # keeps serving; a cancelled return issues one (entry_state).
            if self.data['role'] != 'leader':
                if self.data['owner'] not in (None, self.node):
                    self.data['previous_owner'] = self.data['owner']
                    self.data['unissued'] = True
                    self.data['ready'] = False
                    self.data.pop('ready_at', None)
                # A former leader taking over again keeps its own bundle
                # instead of retiring it under the clients.
                self.data.pop('retire_at', None)
                self.data.update(role='leader', owner=self.node, redirect=None, autonomous=autonomous,
                                 retiring=False)
                self.state.save()
            return
        if self.data['owner'] not in (None, self.node):
            self.data['previous_owner'] = self.data['owner']
        changed = (self.data['owner'] != self.node or self.data.get('credentials_lost', False)
                   or self.data.get('unissued', False))
        resumed = self.data['role'] != 'leader'
        self.data.update(role='leader', owner=self.node, redirect=None, autonomous=autonomous,
                         retiring=False, retired=False)
        # Starting a new local generation replaces the old credentials. Its
        # predecessor's delayed end event must never stop the new service.
        self.data.pop('retire_at', None)
        if changed:
            self.data['generation'] += 1
            self.data['issued_generation'] = self.data['generation']
            self.data['credentials_lost'] = False
            self.data['unissued'] = False
            self.data['ready'] = False
            self.data.pop('ready_at', None)
        if not self.data['emergency_id']:
            # An outage without an entry restart keeps the entry cursor; the
            # ended emergency makes the next identifier (and its queue) new.
            previous = self.data.get('previous_emergency')
            self.data['emergency_id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, self.installation + ':' +
                                                     str(self.state.data.get('entry_cursor', [0, '', 0])[:2]) +
                                                     (':' + previous if previous else '')))
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

    def activate(self):
        """The admin's start link: the outage counts as due now, the election runs as usual."""
        self.data['active'] = True
        self.state.save()

    def bundle_ready(self, now):
        """Called by integration only after connection self-test and issuance."""
        if not self.data.get('ready'):
            self.data['ready_at'] = now
        self.data['ready'] = True
        self.state.save()

    def credentials_lost(self):
        self.data['credentials_lost'] = True
        self.state.save()

    def _retire(self, now):
        if self.data.get('retire_at') is not None and now >= self.data['retire_at']:
            self.events.on_emergency_end()
            del self.data['retire_at']
            self.data['retired'] = True
            self.state.save()

    def entry_state(self, now, ready):
        """Leader only, every round: True ready, False explicitly not, None no fresh answer."""
        if ready:
            self.ready_since = self.ready_since if self.ready_since is not None else now
            self.not_ready = 0
            self.stale_since = None
        else:
            self.ready_since = None
            if ready is False:
                self.not_ready += 1
                self.stale_since = None
            elif self.stale_since is None:
                self.stale_since = now
        if self.data['role'] != 'leader' or not self.data['active']:
            return
        recovery = self.data['recovery']
        wall = self.wall()
        if recovery is None:
            if ready and now - self.ready_since >= STABLE:
                number = self.data.get('recovery_round', 0) + 1
                self.data['recovery_round'] = number
                self.proposed = deadline_after(wall + PROPOSAL_MARGIN)
                self.data['recovery'] = {'round': number, 'confirmed': wall, 'deadline': None,
                                         'proposed': self.proposed}
                self.state.save()
                LOG.info('entry recovered: round %s confirmed after %s s', number, STABLE)
            return
        if self.not_ready >= 2 or (self.stale_since is not None and now - self.stale_since >= MISSED):
            self.data['recovery'] = None
            self.data['cancelled_round'] = recovery['round']
            self.state.save()
            LOG.info('entry recovery cancelled: round %s, entry not ready again', recovery['round'])
            if self.data.get('unissued') or self.data.get('credentials_lost'):
                # A leader that took over during the return has no bundle of
                # its own; the emergency goes on, so it issues one now.
                self._become(self.data.get('autonomous', False))
            return
        if recovery['deadline'] is None:
            self._propose(recovery, wall, now)
        self._deadline_due(wall)

    def _propose(self, recovery, wall, now):
        proposed = recovery.get('proposed')
        if proposed is not None and proposed != self.proposed and wall < proposed - NOTICE + GUARD:
            # Inherited from a leader that may have announced it just before
            # it died: keep its time and announce nothing a second time.
            LOG.info('entry recovery: deadline taken over from the previous leader')
            self.set_deadline(recovery['round'], proposed, now)
        elif proposed is None or wall >= proposed - NOTICE:
            recovery['proposed'] = self.proposed = deadline_after(wall + PROPOSAL_MARGIN)
            self.state.save()

    def proposal(self, now):
        """The proposed deadline once every exit following this leader has it, else None."""
        recovery = self.data['recovery']
        if not recovery or recovery['deadline'] is not None or recovery.get('proposed') is None:
            return None
        for status, at in self.peers.values():
            echoed = status.get('recovery') or {}
            if (now - at < MISSED and status.get('owner') == self.node
                    and (echoed.get('round'), echoed.get('proposed')) != (recovery['round'], recovery['proposed'])):
                return None
        return recovery['proposed']

    def set_deadline(self, number, deadline, now):
        """The first delivered notice (or no channel at all) fixes the deadline."""
        recovery = self.data['recovery']
        if not recovery or recovery['round'] != number or recovery['deadline'] is not None:
            return
        recovery['deadline'] = deadline
        self.state.save()
        LOG.info('entry recovery deadline fixed: round %s', number)
        self.events.on_entry_recovered(now + max(0, deadline - self.wall()))

    def _deadline_due(self, wall):
        recovery = self.data['recovery']
        if not recovery or not self.data['active']:
            return
        if recovery['deadline'] is not None and wall >= recovery['deadline']:
            self.finish('аварийный доступ отключён по сроку')
        elif recovery['deadline'] is None and wall - recovery['confirmed'] >= TELEGRAM_WAIT:
            self.finish('уведомление о возврате не доставлено за 2 часа, завершено без уведомления')

    def finish(self, reason):
        """Every node ends the emergency at the agreed deadline; the agent keeps running."""
        ended = self.data['emergency_id']
        self.events.on_emergency_end()
        self.data.update(role='observer', active=False, owner=None, redirect=None, autonomous=False,
                         emergency_id='', previous_emergency=ended, recovery=None, ready=False,
                         retiring=False, retired=False, pending_leader=False, grant=None, unissued=False,
                         failed_since=None, failed_rounds=0)
        for key in ('retire_at', 'ready_at', 'majority_at', 'previous_owner'):
            self.data.pop(key, None)
        self.candidate = None
        self.ready_since = None
        self.not_ready = 0
        self.stale_since = None
        self.state.save()
        LOG.info('emergency ended: %s', reason)

    def tick(self, now, outage):
        self._retire(now)
        if self.data['role'] != 'leader':
            self._deadline_due(self.wall())
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
                # Followers keep the leader's recovery: a new leader or a
                # restart continues the same deadline.
                self.data['recovery'] = status.get('recovery')
                # Round numbers go on from the inherited one: a repeated
                # number after a cancelled return is taken as already announced.
                if self.data['recovery']:
                    self.data['recovery_round'] = max(self.data.get('recovery_round', 0),
                                                      self.data['recovery']['round'])
                if previous_owner == self.node:
                    self.data['retiring'] = True
                if self.data.get('retiring') and status.get('ready'):
                    # Ready time is an age, never a remote monotonic timestamp.
                    # A merge or a delayed poll must not grant another ten
                    # minutes to credentials whose replacement is already issued.
                    age = status['ready_age'] + now - self.peers[winner][1]
                    self.data.setdefault('retire_at', now + max(0, TRANSITION - age))
                    self.data['retiring'] = False
                    self._retire(now)
                self.data['failed_rounds'] = 0
                self.data['failed_since'] = None
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
                self.data['failed_since'] = None
                self.data['failed_rounds'] = 0
                self.data['majority_at'] = now
                self._become(False)
                return
        if self.data['failed_since'] is None:
            self.data['failed_since'] = now
        if now - self.data['last_round'] >= 15:
            self.data['failed_rounds'] += 1
            self.data['last_round'] = now
        if self.data['failed_rounds'] >= 3 and now - self.data['failed_since'] >= GUARD:
            self._become(True)
        elif self.data['role'] == 'leader':
            majority_at = self.data.get('majority_at')
            if (self.data['failed_rounds'] >= STEP_DOWN_ROUNDS or majority_at is None
                    or now - majority_at >= STEP_DOWN_ROUNDS * PERIOD):
                self._step_down(None)
        elif self.data.get('redirect'):
            self._step_down(None)
