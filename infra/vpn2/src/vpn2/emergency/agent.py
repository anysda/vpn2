"""Аварийный агент: entry шлёт экзитам подписанный пульс, экзиты выбирают лидера.

Лидер - первый по порядку конфига живой экзит, за которого голосует большинство.
Голос обещан до LEASE после последней вести от избранного, поэтому двух лидеров
с большинством не бывает. Экзит без большинства GUARD секунд ведёт аварию сам.
"""

import argparse
import fcntl
import hmac
import json
import logging
import math
from pathlib import Path
import secrets
import signal
import socket
import threading
import time

from .. import fsutil
from ..notify import BotApi
from . import hy2, notices, readiness, snapshot, spool, web

LOG = logging.getLogger(__name__)
ENTRY = '__entry__'
PERIOD = 15          # пульс entry и статус экзита
MISSED = 45          # столько тишины - узел не отвечает
LEASE = 60           # обещание голоса и карантин после старта
OUTAGE = 1200        # тишина entry до аварии
NOTICE_AFTER = 180   # тишина entry до сообщения админу
GUARD = 300          # без большинства до самостоятельной аварии
STABLE = 120         # готовность entry до объявления возврата
RETURN = 600         # от объявления возврата до конца аварии
TRANSITION = 600     # старые учётки после готовности нового лидера
MARGIN = 5           # запас на доставку голоса
FRESH = 300          # допустимое расхождение часов узлов
MAX_PACKET = 2048


def pack(key, message):
    body = json.dumps(message, separators=(',', ':')).encode()
    return hmac.new(key, body, 'sha256').digest() + body


def unpack(key, raw):
    if not 32 < len(raw) <= MAX_PACKET or not hmac.compare_digest(
            hmac.new(key, raw[32:], 'sha256').digest(), raw[:32]):
        return None
    try:
        message = json.loads(raw[32:])
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


class Agent:
    def __init__(self, config, state_dir):
        self.config = config
        self.directory = Path(state_dir)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.installation = config['installation_id']
        self.key = bytes.fromhex(config['key'])
        self.node = config['node']
        self.entry = config.get('entry_id', ENTRY)
        self.nodes = {name: (item['host'], int(item['port'])) for name, item in config['nodes'].items()}
        self.order = [name for name in self.nodes if name != self.entry]
        self.counter = 0
        self.stop = threading.Event()

    def stamp(self):
        self.counter = max(self.counter + 1, time.time_ns())
        return self.counter

    def send(self, sock, target, **message):
        try:
            sock.sendto(pack(self.key, dict(message, i=self.installation, f=self.node, n=self.stamp())),
                        self.nodes[target])
        except OSError:
            pass

    def run_entry(self):
        """Entry: пульс с признаком готовности каждому экзиту."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            while not self.stop.is_set():
                began = time.monotonic()
                ready = readiness.entry_ready()
                for name in self.order:
                    self.send(sock, name, ready=ready)
                self.stop.wait(max(1, PERIOD - (time.monotonic() - began)))


class Exit(Agent):
    def __init__(self, config, state_dir, *, clock=time.monotonic, wall=time.time):
        super().__init__(config, state_dir)
        self.clock, self.wall = clock, wall
        self.majority = len(self.order) // 2 + 1
        self.lock = threading.RLock()
        self.path = self.directory / 'agent.json'
        self.state = fsutil.read_json(self.path, {})
        self.saved = None
        self.started = self.entry_seen = clock()
        self.ready_since = None
        self.not_ready = 0
        self.reach = True
        self.counters = {}
        self.peers = {}
        self.vote = None
        self.promise = (None, 0)
        self.alone_since = None
        self.role = False
        self.autonomous = False
        self.sent = (None, -math.inf)
        # Работник: что уже сделано в этом процессе.
        self.server = None
        self.issued = None
        self.profiles = {}
        self.following = None
        self.retry_at = self.probe_at = self.tls_at = self.sessions_at = self.notice_at = -math.inf
        self.cleanup = None
        if self.state.get('installation') not in (None, self.installation):
            # Entry установлен заново: авария прежней установки кончается здесь.
            old = self.state.get('leader', {}).get('emergency')
            told = sorted(notices.informed(self.directory, old)) if old else []
            self.state = {'generation': self.state.get('generation', 0),
                          'reinstalled': {'emergency': old, 'clients': told} if told else None}
            self.cleanup = old or 'foreign'
        self.state['installation'] = self.installation
        self.state.setdefault('leader', {'active': False})
        self.state.setdefault('generation', 0)
        self.save()

    # Состояние.

    @property
    def leader(self):
        return self.state['leader']

    def save(self):
        raw = json.dumps(self.state, separators=(',', ':'))
        if raw != self.saved:
            fsutil.atomic(self.path, raw)
            self.saved = raw

    def image(self):
        value = snapshot.load(self.directory, self.wall())
        return value if value and value['installation_id'] == self.installation else None

    def exit_name(self, image):
        return next((item['name'] for item in (image or {}).get('exits', []) if item['id'] == self.node),
                    self.node)

    def alive(self, now):
        return [name for name in self.order
                if name == self.node or (name in self.peers and now - self.peers[name]['seen'] < MISSED)]

    def silence(self, now):
        """Тишина entry: хватит того, что его слышит любой живой экзит."""
        heard = [self.peers[name]['entry'] for name in self.alive(now) if name != self.node]
        return now - min(now, max([self.entry_seen, *heard]))

    # Сеть и выборы: основной поток.

    def on_packet(self, raw, address, now):
        message = unpack(self.key, raw)
        if not message or message.get('i') != self.installation:
            return
        sender, counter = message.get('f'), message.get('n')
        if (sender not in self.nodes or sender == self.node or type(counter) is not int
                or counter <= self.counters.get(sender, 0)
                or abs(counter - time.time_ns()) > FRESH * 10 ** 9):
            return
        self.counters[sender] = counter
        if sender == self.entry:
            self.entry_seen = now
            if message.get('ready') is True:
                self.not_ready = 0
                self.ready_since = now if self.ready_since is None else self.ready_since
            else:
                self.not_ready += 1
                self.ready_since = None
            known = self.state.get('entry_host') or self.nodes[self.entry][0]
            if address[0] != known:
                self.state['entry_host'] = address[0]
                self.state['moved'] = address[0] if address[0] != self.nodes[self.entry][0] else None
            return
        try:
            self.peers[sender] = dict(
                seen=now, entry=now - float(message['age']), vote=message.get('vote'),
                until=now + float(message.get('lease', 0)) - MARGIN, claim=message.get('claim'),
                em=message.get('em'), ended=message.get('ended'), start=message.get('start') is True)
        except (KeyError, TypeError, ValueError):
            return
        self.learn(sender, self.peers[sender])

    def learn(self, sender, peer):
        """Авария, известная соседу: лидеру верим целиком, остальным - что она идёт."""
        leader, claim, known = self.leader, peer['claim'], peer['em']
        if peer['ended'] and peer['ended'] == leader.get('emergency') and leader['active']:
            return self.finish()
        if isinstance(claim, dict) and claim.get('active') and not self.role:
            known = claim
        if not isinstance(known, dict) or known.get('e') in (None, self.state.get('ended')):
            return
        number = int(known.get('g', 0))
        self.state['generation'] = max(self.state['generation'], number)
        if known is claim and (number, known['e']) >= (leader.get('generation', 0), leader.get('emergency', '')):
            self.state['leader'] = dict(active=True, emergency=known['e'], generation=number, owner=sender,
                                        ready=known.get('ready') is True, deadline=known.get('deadline'),
                                        event=leader.get('event'), told=leader.get('told'))
        elif not leader['active']:
            self.state['leader'] = dict(active=True, emergency=known['e'], generation=number, owner=None,
                                        ready=False, deadline=known.get('deadline'))

    def claims(self, now):
        found = {name: self.peers[name]['claim'] for name in self.alive(now)
                 if name != self.node and isinstance(self.peers[name]['claim'], dict)}
        if self.role:
            found[self.node] = {'auto': self.autonomous}
        return found

    def tick(self, now):
        alive = self.alive(now)
        silence = self.silence(now)
        if now - self.entry_seen >= MISSED:
            self.ready_since = None
        wanted = (self.leader['active'] or silence >= MISSED
                  or any(self.peers[name]['em'] for name in alive if name != self.node))
        quarantine = now - self.started < LEASE
        choice = None
        if wanted and not quarantine:
            claims = self.claims(now)
            if claims:
                choice = min(claims, key=lambda name: (claims[name].get('auto') is True, self.order.index(name)))
            else:
                choice = self.vote if self.vote in alive else alive[0]
        held, until = self.promise
        if held not in (None, choice) and now < until:
            choice = held if wanted and not quarantine else None
        self.vote = choice
        if choice not in (None, self.node):
            self.promise = (choice, self.peers[choice]['seen'] + LEASE if choice in self.peers else until)
        votes = sum(1 for name in alive if name != self.node and self.peers[name]['vote'] == self.node
                    and now < self.peers[name]['until'])
        lonely = wanted and not quarantine and len(alive) < self.majority
        self.alone_since = (self.alone_since if self.alone_since is not None else now) if lonely else None
        elected = choice == self.node and votes + 1 >= self.majority
        alone = lonely and choice == self.node and now - self.alone_since >= GUARD
        if (elected or alone, alone and not elected) != (self.role, self.autonomous):
            self.role, self.autonomous = elected or alone, alone and not elected
            LOG.info('role=%s autonomous=%s', 'leader' if self.role else 'follower', self.autonomous)
        if self.role and self.leader['active'] and self.leader.get('owner') != self.node:
            self.take('leader_changed', self.leader['emergency'], self.leader.get('deadline'))
        self.lead(now, silence, alive)
        signature = (self.vote, self.role, json.dumps(self.leader, sort_keys=True))
        if signature != self.sent[0] or now - self.sent[1] >= PERIOD:
            self.sent = (signature, now)
            return True
        return False

    def take(self, event, emergency, deadline=None):
        self.state['generation'] += 1
        self.state['leader'] = dict(active=True, emergency=emergency, generation=self.state['generation'],
                                    owner=self.node, ready=False, deadline=deadline, event=event)
        self.state.pop('start', None)

    def lead(self, now, silence, alive):
        leader, wall = self.leader, self.wall()
        if not leader['active']:
            started = self.state.get('start') or any(self.peers[name]['start'] for name in alive
                                                     if name != self.node)
            if self.role and silence >= MISSED and (silence >= OUTAGE or started):
                self.take('initial', secrets.token_hex(8))
            elif silence < MISSED:
                self.state.pop('start', None)
            return
        if self.role:
            quiet = silence >= MISSED or self.not_ready >= 2 or not self.reach
            if leader.get('deadline') is None:
                if not quiet and self.ready_since is not None and now - self.ready_since >= STABLE:
                    leader['deadline'] = math.ceil((wall + RETURN) / 60) * 60
            elif quiet:
                leader['deadline'] = None
        if leader.get('deadline') is not None and wall >= leader['deadline']:
            self.finish()

    def finish(self):
        self.cleanup = self.state['ended'] = self.leader.get('emergency')
        self.state['leader'] = {'active': False}
        self.state.pop('start', None)
        self.state.pop('moved', None)
        self.reach = True

    def status(self, now):
        leader = self.leader
        known = dict(e=leader['emergency'], g=leader['generation'], deadline=leader.get('deadline'),
                     active=True, ready=leader.get('ready') is True, auto=self.autonomous) \
            if leader['active'] else None
        claim = (known or {'active': False, 'auto': self.autonomous}) if self.role else None
        return dict(age=round(self.silence(now), 1), vote=self.vote,
                    lease=round(max(0, self.promise[1] - now), 1) if self.vote != self.node else LEASE,
                    claim=claim, em=known, ended=self.state.get('ended'),
                    start=bool(self.state.get('start')))

    def run(self):
        threading.Thread(target=self.worker, name='emergency-worker', daemon=True).start()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((self.config.get('bind', '0.0.0.0'), self.nodes[self.node][1]))
            sock.settimeout(1)
            while not self.stop.is_set():
                for _ in range(64):
                    try:
                        raw, address = sock.recvfrom(MAX_PACKET + 1)
                    except OSError:
                        break
                    with self.lock:
                        self.on_packet(raw, address, self.clock())
                    sock.settimeout(0)
                sock.settimeout(1)
                with self.lock:
                    now = self.clock()
                    message = self.status(now) if self.tick(now) else None
                    self.save()
                for name in self.order if message else ():
                    if name != self.node:
                        self.send(sock, name, **message)

    # Работник: Hysteria2, панель, Telegram.

    def worker(self):
        while not self.stop.wait(2):
            try:
                self.work(self.clock())
            except Exception:
                LOG.exception('emergency worker pass failed')

    def panel(self, link_only=False):
        if self.server is not None:
            self.server.refresh_tls()
        else:
            server = web.make_server(
                self.directory, exit_id=self.node, public_ip=self.nodes[self.node][0],
                snapshot_loader=lambda _directory: self.image(),
                profiles=lambda generation: self.profiles if self.issued and self.issued[1] == generation else {},
                connections=lambda generation: hy2.connections(self.directory, generation),
                broadcast=self.broadcast, start=self.start_link)
            if link_only:
                # Пароль на диске остался от прошлой аварии: до новой он не действует.
                server.panel.on_step_down()
            server.panel.link_only = link_only
            threading.Thread(target=server.serve_forever, name='emergency-panel', daemon=True).start()
            self.server = server
        return self.server.panel

    def close_panel(self):
        server, self.server = self.server, None
        if server:
            server.shutdown()
            server.server_close()

    def api(self, image):
        return BotApi(image['bot_token']) if image and image.get('bot_token') else None

    def work(self, now):
        with self.lock:
            leader, role, autonomous = dict(self.leader), self.role, self.autonomous
            silence, cleanup, self.cleanup = self.silence(now), self.cleanup, None
        image, wall = self.image(), self.wall()
        api = self.api(image)
        if cleanup:
            self.end(cleanup)
        self.reinstalled(image)
        self.notice(now, wall, image, api, role and not leader['active'], silence, leader['active'])
        if leader['active']:
            if now - self.probe_at >= PERIOD:
                self.probe_at = now
                self.reach = readiness.reachable(self.state.get('entry_host') or self.nodes[self.entry][0])
                if not self.reach:
                    self.ready_since = None
            if role and leader['owner'] == self.node:
                self.serve(now, wall, leader, autonomous, image)
            elif not role and leader.get('owner') != self.node:
                self.follow(wall, leader)
        if api and (role or not leader['active']):
            spool.flush(self.directory, api, wall)
        if now - self.tls_at >= 60 and (self.server or self.issued):
            self.tls_at = now
            hy2.refresh_tls(hy2.home(self.directory), self.nodes[self.node][0])
            if self.server:
                self.server.refresh_tls()

    def serve(self, now, wall, leader, autonomous, image):
        """Лидер: выдача этого поколения, затем сообщения о возврате и его отмене."""
        emergency, generation = leader['emergency'], leader['generation']
        common = dict(emergency=emergency, generation=generation, exit_name=self.exit_name(image), wall=wall)
        if self.issued != (emergency, generation):
            state = hy2.read_state(hy2.home(self.directory))
            resumed = state['active'] and state['generation'] == generation
            # Новый лидер при объявленном возврате ничего не выдаёт: авария кончается.
            if (leader.get('deadline') is not None and not resumed) or now < self.retry_at or image is None:
                return
            try:
                spool.clear(self.directory, keep_emergency=emergency)
                for old in {row['generation'] for row in spool.rows(self.directory)} - {generation}:
                    spool.cancel(self.directory, generation=old)
                devices = [device for client in image['clients'] if client['allowed']
                           for device in client['devices']]
                self.profiles = hy2.start(self.directory, generation, devices)
                panel = self.panel()
                event = leader.get('event') or 'initial'
                payload = panel.on_emergency_leader(emergency, generation, autonomous, event)
                notices.admin(self.directory, image, event, notices.panel_text(payload, event), **common)
                notices.clients(self.directory, image, event, profiles=self.profiles, url=panel.url, **common)
            except Exception:
                LOG.exception('emergency issuance failed: generation %s', generation)
                self.retry_at = now + 30
                return
            self.issued = (emergency, generation)
            self.following = None
            with self.lock:
                self.state.pop('retire', None)
            LOG.info('emergency access issued: generation %s, %s devices', generation, len(self.profiles))
            with self.lock:
                if self.leader.get('generation') == generation:
                    self.leader['ready'] = True
        if now - self.sessions_at >= 12:
            self.sessions_at = now
            hy2.poll_sessions(self.directory)
        deadline, told = leader.get('deadline'), leader.get('told')
        if image is None or deadline == told:
            return
        panel = self.panel()
        if deadline is not None:
            text = notices.ADMIN_RECOVERED.format(deadline=notices.clock(deadline))
            if self.state.get('moved'):
                text += notices.ADMIN_ENTRY_MOVED.format(address=self.state['moved'])
            panel.on_entry_recovered(time.monotonic() + deadline - wall, deadline)
            notices.admin(self.directory, image, 'recovered', text, version=deadline, **common)
            notices.clients(self.directory, image, 'recovered', version=deadline,
                            only=notices.informed(self.directory, emergency),
                            deadline=notices.clock(deadline), **common)
        else:
            heard = {row['client'] for row in spool.rows(self.directory, emergency=emergency, event='recovered',
                                                         state='sent', audience='client')
                     if row['key'].split(':')[2] == str(told)}
            spool.cancel(self.directory, emergency=emergency, event='recovered')
            panel.on_return_cancelled()
            notices.admin(self.directory, image, 'return_cancelled', notices.ADMIN_RETURN_CANCELLED,
                          version=told, **common)
            notices.clients(self.directory, image, 'return_cancelled', version=told, only=heard, **common)
        with self.lock:
            if self.leader.get('generation') == generation:
                self.leader['told'] = deadline

    def follow(self, wall, leader):
        """Не лидер: панель ведёт к лидеру, своя прежняя выдача доживает TRANSITION."""
        owner = leader.get('owner')
        if owner and self.following != owner:
            self.panel().on_step_down(owner)
            spool.cancel(self.directory, emergency=leader['emergency'])
            self.following = owner
        if not hy2.read_state(hy2.home(self.directory))['active']:
            return
        retire = self.state.get('retire')
        if retire is None and leader.get('ready'):
            with self.lock:
                self.state['retire'] = wall + TRANSITION
        elif retire is not None and wall >= retire:
            hy2.stop(self.directory)
            self.issued, self.profiles = None, {}
            with self.lock:
                self.state.pop('retire', None)
            LOG.info('previous emergency credentials retired')

    def end(self, emergency):
        self.close_panel()
        hy2.stop(self.directory)
        self.issued, self.profiles, self.following = None, {}, None
        with self.lock:
            self.state.pop('retire', None)
        if emergency == 'foreign':
            spool.clear(self.directory)
        else:
            spool.cancel(self.directory, emergency=emergency)
        port = hy2.settings()['port']
        if hy2.listening(port):
            LOG.error('emergency end: Hysteria2 port %s still listening', port)
        else:
            LOG.info('emergency end: credentials revoked, Hysteria2 port %s closed, panel stopped', port)

    def reinstalled(self, image):
        pending = self.state.get('reinstalled')
        if pending and image and image['clients']:
            notices.clients(self.directory, image, 'reinstalled', emergency=pending['emergency'], generation=0,
                            exit_name=self.exit_name(image), wall=self.wall(), only=set(pending['clients']))
            with self.lock:
                self.state['reinstalled'] = None

    def broadcast(self, text, generation):
        """Поток панели: только постановка, отправит следующий проход работника."""
        image = self.image() or {}
        notices.clients(self.directory, image, 'broadcast', text=text, emergency=self.leader['emergency'],
                        generation=generation, version=secrets.token_hex(8),
                        exit_name=self.exit_name(image), wall=self.wall())

    # Сообщение админу о тишине entry и ссылка «Запустить сейчас».

    def signature(self, nonce, expires):
        text = f'vpn2-emergency-start\n{self.installation}\n{self.node}\n{nonce}\n{expires}'
        return hmac.new(self.key, text.encode(), 'sha256').hexdigest()

    def notice(self, now, wall, image, api, coordinator, silence, active):
        sent = self.state.get('notice')
        if sent and (active or silence < MISSED):
            if not active and sent.get('message_id') and api:
                reply = api.call(image['admin_chat_id'], {'method': 'editMessageText', 'text': notices.OUTAGE_BACK,
                                                          'message_id': sent['message_id']})
                if not (reply.ok or reply.final):
                    return
            with self.lock:
                self.state.pop('notice', None)
            if self.server and self.server.panel.link_only:
                self.close_panel()
        elif (coordinator and not sent and NOTICE_AFTER <= silence < OUTAGE and api
              and image['admin_chat_id'] is not None and now >= self.notice_at):
            nonce, expires = secrets.token_hex(8), int(wall + OUTAGE - silence)
            url = f'{self.panel(link_only=True).url}/start/{nonce}.{expires}.{self.signature(nonce, expires)}'
            reply = api.call(image['admin_chat_id'], {
                'method': 'sendMessage',
                'text': notices.OUTAGE.format(minutes=math.ceil((OUTAGE - silence) / 60)),
                'reply_markup': {'inline_keyboard': [[{'text': notices.OUTAGE_BUTTON, 'url': url}]]}})
            if reply.ok:
                with self.lock:
                    self.state['notice'] = {'nonce': nonce, 'expires': expires,
                                            'message_id': (reply.result or {}).get('message_id')}
            else:
                LOG.warning('entry silent: admin notice not sent: %s', reply.reason)
                self.notice_at = now + 30 if not reply.final else math.inf
                self.close_panel()

    def start_link(self, token):
        """Поток панели: ключ страницы web.START_PAGES."""
        nonce, _, rest = token.partition('.')
        expires, _, sign = rest.partition('.')
        if not hmac.compare_digest(sign, self.signature(nonce, expires)):
            return 'invalid'
        with self.lock:
            sent = self.state.get('notice') or {}
            if self.silence(self.clock()) < MISSED and not self.leader['active']:
                return 'entry_back'
            if (self.leader['active'] or sent.get('nonce') != nonce or sent.get('used')
                    or not expires.isdigit() or self.wall() > int(expires)):
                return 'gone'
            sent['used'] = self.state['start'] = True
        return 'starting'


def main(argv=None):
    parser = argparse.ArgumentParser(prog='vpn2 emergency agent')
    parser.add_argument('--config', required=True)
    parser.add_argument('--state-dir', default='/var/lib/vpn2-emergency')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    config = json.loads(Path(args.config).read_text())
    if len(config['nodes']) > 65 or config['node'] not in config['nodes']:
        raise SystemExit('emergency agent: invalid config')
    entry = config['node'] == config.get('entry_id', ENTRY)
    agent = Agent(config, args.state_dir) if entry else Exit(config, args.state_dir)
    with open(agent.directory / 'agent.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit('observer already running') from None
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_: agent.stop.set())
        agent.run_entry() if entry else agent.run()
    return 0
