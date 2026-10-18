"""Authenticated challenge/response with direct (never transitive) evidence.

The wire contains only liveness/election metadata. HMAC protects integrity and
identity; snapshots, credentials and client data must not use this transport.
Each response binds a fresh unpredictable request, including after restart.
"""

import hashlib
import hmac
import json
import math
import secrets

PERIOD = 15
MISSED = 45
OUTAGE = 1200
MAX_PACKET = 16384


def encode(key: bytes, message: dict) -> bytes:
    body = json.dumps(message, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    packet = hmac.new(key, body, hashlib.sha256).hexdigest().encode() + b'\n' + body
    if len(packet) > MAX_PACKET:
        raise ValueError('packet exceeds limit')
    return packet


def decode(key: bytes, packet: bytes) -> dict:
    if len(packet) > MAX_PACKET:
        raise ValueError('packet exceeds limit')
    signature, body = packet.split(b'\n', 1)
    expected = hmac.new(key, body, hashlib.sha256).hexdigest().encode()
    if not hmac.compare_digest(signature, expected):
        raise ValueError('invalid authentication')
    message = json.loads(body)
    if not isinstance(message, dict):
        raise ValueError('invalid message')
    return message


class Heartbeat:
    def __init__(self, state, now):
        self.state = state
        self.pending = {}
        self.observations = {}
        if not state.same_boot:
            state.data.pop('direct', None)
            state.data['last_entry'] = now
        self.last_entry = state.data.setdefault('last_entry', now)

    def challenge(self, peer, now):
        nonce = secrets.token_hex(24)
        self.pending[peer] = (nonce, now)
        return nonce

    def response(self, peer, nonce, now):
        pending = self.pending.get(peer)
        if not pending or pending[0] != nonce or not 0 <= now - pending[1] <= PERIOD:
            return None
        del self.pending[peer]
        return now - pending[1]

    def receive(self, evidence, now, transit=0, direct=False, observer='entry'):
        if not isinstance(evidence, dict):
            return False
        run = evidence.get('run')
        sequence = evidence.get('sequence')
        epoch = evidence.get('epoch')
        age = evidence.get('age')
        if (not isinstance(run, str) or not 1 <= len(run) <= 64
                or type(sequence) is not int or sequence < 1
                or type(epoch) is not int or epoch < 1
                or type(age) not in (float, int) or not math.isfinite(age)
                or age < 0 or age + transit >= MISSED):
            return False
        newest = self.state.data.get('entry_cursor', [0, '', 0])
        cursor = [epoch, run, sequence]
        if epoch < newest[0] or (epoch == newest[0] and run != newest[1]):
            return False
        previous = self.observations.get(observer)
        if previous and cursor <= previous:
            return False
        self.observations[observer] = cursor
        # A replay first observed via another exit cannot extend its original
        # observation time. The direct receiver exports elapsed age each time.
        at = now - age - transit
        if cursor > newest:
            self.state.data['entry_cursor'] = cursor
            self.last_entry = max(self.last_entry, at)
        elif cursor == newest:
            at = min(at, self.last_entry)
        else:
            return False
        self.state.data['last_entry'] = self.last_entry
        if direct:
            self.state.data['direct'] = dict(epoch=epoch, run=run, sequence=sequence, at=at)
        return True

    def direct_evidence(self, now):
        direct = self.state.data.get('direct')
        if not direct:
            return None
        return {k: direct[k] for k in ('epoch', 'run', 'sequence')} | {
            'age': max(0, now - direct['at'])}

    def poll_due(self, now):
        direct = self.state.data.get('direct')
        return direct is None or now - direct['at'] >= MISSED

    def emergency_due(self, now):
        return now - self.last_entry >= OUTAGE
