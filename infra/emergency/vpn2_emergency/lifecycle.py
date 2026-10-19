"""Emergency side effects of the observer: Hysteria2, the panel and Telegram.

Slow work (Hysteria2 start, Telegram passes, readiness probes) runs on one
worker thread in submission order. Its results are applied on the agent loop,
which alone changes election state, so a slow Bot API never delays a round.
"""

from datetime import datetime, timedelta, timezone
import logging
import math
import queue
import socket
import subprocess
import threading
from urllib import request

from . import hooks, hy2, tgqueue, web
from .leader import NOTICE

LOG = logging.getLogger(__name__)
# The snapshot carries no time zone; notices and the panel name one explicitly.
ZONE = timezone(timedelta(hours=3))
ENTRY_UNITS = ('sing-box', 'wg-quick@wg0', 'openvpn-server@server', 'strongswan-starter',
               'anysda-ikev2-routing')
# The panel answers only once its database is restored and configs applied.
PANEL_URL = 'http://127.0.0.1:51821/api/version'
# Control traffic: the entry's own proxy leaves through the exits.
SOCKS = ('127.0.0.1', 7897)
CONTROL_TARGET = ('cp.cloudflare.com', 80)
ENTRY_PORT = 443


def iso(wall):
    return datetime.fromtimestamp(wall, ZONE).isoformat(timespec='seconds')


def deadline_after(wall):
    """Whole minute, so the notice and the panel show the same time."""
    return math.ceil((wall + NOTICE) / 60) * 60


def socks_connect(proxy, target, timeout=5):
    host, port = target
    name = host.encode()
    with socket.create_connection(proxy, timeout=timeout) as sock:
        sock.sendall(b'\5\1\0')
        if sock.recv(2) != b'\5\0':
            return False
        sock.sendall(b'\5\1\0\3' + bytes([len(name)]) + name + port.to_bytes(2, 'big'))
        reply = sock.recv(10)
        return len(reply) >= 2 and reply[1] == 0


def entry_ready(settings):
    """Entry: VPN units active, the panel answers, control traffic passes."""
    units = settings.get('units', ENTRY_UNITS)
    try:
        states = subprocess.run(['systemctl', 'is-active', *units], capture_output=True,
                                text=True, timeout=10).stdout.split()
        if states != ['active'] * len(units):
            return False
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(settings.get('panel', PANEL_URL), timeout=5) as response:
            if response.status != 200:
                return False
        return socks_connect(tuple(settings.get('socks', SOCKS)),
                             tuple(settings.get('target', CONTROL_TARGET)))
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def reachable(host, port, timeout=5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Lifecycle:
    def __init__(self, agent, *, hy2_module=hy2, web_module=web, tg=tgqueue, threaded=True,
                 panel_address=('0.0.0.0', 443), hy2_options=None,
                 api_base='https://api.telegram.org', probe_ready=None, probe_entry=None):
        self.agent = agent
        self.directory = agent.state.directory
        self.hy2 = hy2_module
        self.web = web_module
        self.tg = tg
        self.panel_address = panel_address
        self.hy2_options = hy2_options
        self.api_base = api_base
        settings = agent.config.get('recovery', {})
        port = settings.get('entry_port', ENTRY_PORT)
        self.probe_ready = probe_ready or (lambda: entry_ready(settings))
        self.probe_entry = probe_entry or (lambda host: reachable(host, port))
        # Until the exit's own check answers, the entry does not count as ready.
        agent.entry_check = (False, -math.inf)
        self.server = None
        self.profiles = {}
        self.pending = set()
        self.restored = False
        self.results = queue.SimpleQueue()
        self.jobs = None
        if threaded:
            self.jobs = queue.Queue()
            self.worker = threading.Thread(target=self._work, name='emergency-lifecycle', daemon=True)
            self.worker.start()

    @property
    def record(self):
        return self.agent.state.data.setdefault('lifecycle', {})

    def attach(self):
        for name in ('on_emergency_leader', 'on_step_down', 'on_entry_recovered', 'on_emergency_end'):
            hooks.subscribe(name, getattr(self, name))

    def close(self):
        if self.jobs is not None:
            self.jobs.put(None)
            self.worker.join(30)
        self._close_server()

    def submit(self, key, function, *args):
        if key in self.pending:
            return
        self.pending.add(key)
        if self.jobs is None:
            self._run(key, function, args)
        else:
            self.jobs.put((key, function, args))

    def _work(self):
        while (job := self.jobs.get()) is not None:
            self._run(*job)

    def _run(self, key, function, args):
        try:
            result = function(*args)
        except Exception:
            LOG.exception('emergency %s failed', key.split(':')[0])
            result = None
        self.results.put((key, result))

    # Agent loop.

    def collect(self, now):
        """Apply finished probes before the round reads them, not a round later."""
        while True:
            try:
                key, result = self.results.get_nowait()
            except queue.Empty:
                break
            self.pending.discard(key)
            if result:
                self._apply(now, *result)

    def tick(self, now):
        self.collect(now)
        agent = self.agent
        if agent.node == agent.entry:
            self.submit('ready', self._ready)
            return
        leader = agent.leader
        if leader is None or not leader.data['active']:
            return
        self.submit('entry', self._check_entry, agent.addresses[agent.entry][0], now)
        data = leader.data
        record = self.record
        if not self.restored:
            # A restarted leader resumes its issuance: same generation, same
            # password, Telegram rows already queued stay deduplicated.
            self.restored = True
            if (data['role'] == 'leader' and record.get('emergency') == data['emergency_id']
                    and record.get('generation') == data.get('issued_generation')):
                self.submit(f"issue:{record['generation']}", self._issue, data['emergency_id'],
                            record['generation'], record.get('autonomous', False), record['event'])
        if data['role'] != 'leader':
            return
        recovery = data['recovery']
        if recovery and record.get('notified') != recovery['round']:
            record['notified'] = recovery['round']
            agent.state.save()
            # An inherited deadline was already announced by the old leader.
            if recovery['deadline'] is None:
                moved = agent.state.data.get('entry_moved') or {}
                address = moved['to'][0] if moved.get('emergency') == data['emergency_id'] else None
                self.submit(f"recovered:{recovery['round']}", self._recovered, data['emergency_id'],
                            data['generation'], recovery['round'], recovery['confirmed'], address)
        cancelled = data.get('cancelled_round')
        if (recovery is None and cancelled and record.get('notified') == cancelled
                and record.get('cancelled') != cancelled):
            record['cancelled'] = cancelled
            agent.state.save()
            if self.server:
                self.server.panel.on_return_cancelled()
            self.submit(f'cancelled:{cancelled}', self._cancelled, data['emergency_id'],
                        data['generation'], cancelled)
        deadline, round_ = None, None
        if recovery:
            round_ = recovery['round'] if recovery['deadline'] is None else None
            deadline = recovery['deadline'] or deadline_after(agent.wall())
        self.submit('deliver', self._deliver, data['emergency_id'], deadline, round_)

    def _apply(self, now, kind, *args):
        agent = self.agent
        leader = agent.leader
        if kind == 'ready':
            agent.ready = args[0]
        elif kind == 'entry':
            agent.entry_check = args
        elif leader is None:
            return
        elif kind in ('issued', 'failed'):
            emergency_id, generation = args
            if (leader.data['role'] == 'leader' and leader.data['emergency_id'] == emergency_id
                    and leader.data.get('issued_generation') == generation):
                if kind == 'issued':
                    leader.bundle_ready(now)
                else:
                    # A new generation replaces what could not be issued.
                    leader.credentials_lost()
        elif kind == 'delivered':
            leader.set_deadline(*args, now)

    # Leader events, on the agent loop.

    def on_emergency_leader(self, emergency_id, generation, autonomous):
        leader = self.agent.leader
        record = self.record
        if (record.get('emergency'), record.get('generation')) == (emergency_id, generation):
            # A resumed issuance (restart, reboot) repeats nothing already queued.
            event = record['event']
        elif record.get('emergency') == emergency_id or leader.data.get('previous_owner'):
            event = 'leader_changed'
        else:
            event = 'initial'
        self.agent.state.data['lifecycle'] = dict(emergency=emergency_id, generation=generation,
                                                  event=event, autonomous=autonomous)
        self.agent.state.save()
        self.restored = True
        self.submit(f'issue:{generation}', self._issue, emergency_id, generation, autonomous, event)

    def on_step_down(self, winner):
        if self.server:
            self.server.panel.on_step_down(winner)
        record = self.record
        if record.get('emergency'):
            self.submit(f"cancel:{record['generation']}", self.tg.cancel, self.directory,
                        emergency_id=record['emergency'], generation=record['generation'],
                        events=['initial', 'leader_changed'])

    def on_entry_recovered(self, deadline_monotonic):
        recovery = self.agent.leader.data['recovery'] if self.agent.leader else None
        if self.server and recovery:
            self.server.panel.on_entry_recovered(deadline_monotonic, recovery['deadline'])

    def on_emergency_end(self):
        emergency_id = self.agent.leader.data['emergency_id'] if self.agent.leader else ''
        if self.server:
            self.server.panel.on_emergency_end()
        self.agent.state.data['lifecycle'] = {}
        self.agent.state.save()
        self.submit(f'end:{emergency_id}', self._end, emergency_id)

    # Worker.

    def _ready(self):
        return ('ready', bool(self.probe_ready()))

    def _check_entry(self, host, now):
        return ('entry', bool(self.probe_entry(host)), now)

    def _panel(self):
        if self.server is None:
            agent = self.agent
            server = self.web.make_server(
                self.directory, exit_id=agent.node, public_ip=agent.addresses[agent.node][0],
                address=self.panel_address, profiles=lambda generation: self.profiles.get(generation, {}))
            threading.Thread(target=server.serve_forever, name='emergency-panel', daemon=True).start()
            self.server = server
        return self.server.panel

    def _close_server(self):
        server, self.server = self.server, None
        if server:
            server.shutdown()
            server.server_close()

    def _exit_name(self):
        snapshot = self.agent.snapshot_loader(self.directory) or {}
        return next((item['name'] for item in snapshot.get('exits', [])
                     if item['id'] == self.agent.node), self.agent.node)

    def _issue(self, emergency_id, generation, autonomous, event):
        try:
            self.tg.prune(self.directory, keep_emergency_id=emergency_id)
            snapshot = self.agent.snapshot_loader(self.directory) or {}
            devices = [dict(id=device['id'], name=device['name'], allowed=client.get('allowed') is True,
                            expires_at=client.get('expires_at'))
                       for client in snapshot.get('clients', []) for device in client.get('devices', [])]
            profiles = self.hy2.start(self.directory, generation, devices, options=self.hy2_options)
            self.profiles = {generation: profiles}
            panel = self._panel()
            admin = panel.on_emergency_leader(emergency_id, generation, autonomous)
            issued_at = iso(self.agent.wall())
            self.tg.enqueue_admin(self.directory, **admin, bundle_version=generation,
                                  issued_at=issued_at, event=event)
            self.tg.enqueue_clients(self.directory, emergency_id=emergency_id, generation=generation,
                                    bundle_version=generation, exit_name=admin['exit_name'],
                                    issued_at=issued_at, exit_url=panel.url, profiles=profiles,
                                    event=event)
        except Exception:
            LOG.exception('emergency issuance failed: generation %s', generation)
            return ('failed', emergency_id, generation)
        LOG.info('emergency access issued: generation %s, %s devices', generation, len(profiles))
        return ('issued', emergency_id, generation)

    def _rows(self, emergency_id, number, audience='client'):
        return [row for row in self.tg.delivery_status(self.directory, audience)
                if row['emergency'] == emergency_id and row['event'] == 'recovered'
                and row['version'] == str(number)]

    def _recovered(self, emergency_id, generation, number, confirmed, address):
        self.tg.cancel(self.directory, emergency_id=emergency_id, events=['initial', 'leader_changed'])
        common = dict(emergency_id=emergency_id, generation=generation, bundle_version=number,
                      exit_name=self._exit_name(), issued_at=iso(self.agent.wall()), event='recovered')
        self.tg.enqueue_admin(self.directory, entry_address=address, **common)
        self.tg.enqueue_clients(self.directory, exit_url=self._panel().url, **common)
        if address:
            LOG.info('entry recovered on a new address: %s', address)
        rows = self._rows(emergency_id, number, 'admin') + self._rows(emergency_id, number)
        if all(row['state'] == 'blocked' for row in rows):
            # Nobody can be told: the clock starts at confirmation.
            LOG.info('entry recovery: no delivery channel, deadline from confirmation')
            return ('delivered', number, deadline_after(confirmed))
        return None

    def _deliver(self, emergency_id, deadline, number):
        self.tg.run_once(self.directory, api_base=self.api_base, now=self.agent.wall(),
                         deadline=iso(deadline) if deadline else None)
        if number is None:
            return None
        rows = self._rows(emergency_id, number, 'admin') + self._rows(emergency_id, number)
        return ('delivered', number, deadline) if any(row['sent'] for row in rows) else None

    def _cancelled(self, emergency_id, generation, number):
        self.tg.cancel(self.directory, emergency_id=emergency_id, events=['recovered'])
        common = dict(emergency_id=emergency_id, generation=generation, bundle_version=number,
                      exit_name=self._exit_name(), issued_at=iso(self.agent.wall()),
                      event='return_cancelled')
        # Only those who were told about the return hear that it is off.
        clients = [row['client'] for row in self._rows(emergency_id, number) if row['sent']]
        if clients:
            self.tg.enqueue_clients(self.directory, clients=clients, exit_url=self._panel().url, **common)
        if any(row['sent'] for row in self._rows(emergency_id, number, 'admin')):
            self.tg.enqueue_admin(self.directory, **common)
        LOG.info('entry recovery cancelled: notice queued for %s clients', len(clients))

    def _end(self, emergency_id):
        self._close_server()
        self.hy2.stop(self.directory)
        self.profiles = {}
        port = self.hy2.settings(self.hy2_options)['port']
        if self.hy2.listening(port):
            LOG.error('emergency end: Hysteria2 port %s still listening', port)
        else:
            LOG.info('emergency end: credentials revoked, Hysteria2 port %s closed, panel stopped', port)
        if emergency_id:
            self.tg.cancel(self.directory, emergency_id=emergency_id)
