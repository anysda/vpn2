"""Generation-scoped emergency Hysteria2; no dependency on the agent event loop.

start/stop/selftest are called by the leader hooks. devices contains authorised
snapshot devices with id/name (or allowed/expires_at for an additional check).
Installation and event wiring are deliberately separate from this module.
"""

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import ipaddress
import json
import logging
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, urlencode


STATE = Path('/var/lib/vpn2-emergency')
OPTIONS = Path('/etc/vpn2/emergency-hy2.json')
UNIT = 'vpn2-emergency-hy2.service'
PROBE_UNIT = 'vpn2-emergency-hy2-probe.service'
MOUNT = Path('/run/vpn2-emergency-hy2')
LOG = logging.getLogger(__name__)
# Sessions: the instance's own Clash API on loopback TCP (same number as the
# QUIC port) lists live streams; every device has its own route rule, so the
# rule name carries the Hysteria2 user. Polled from the agent process.
SESSIONS_EVERY = 12
SESSIONS_STALE = 40
DEVICE_USER = re.compile(r'g(\d+)-d(\d+)')
SESSION_RULE = re.compile(rb'"rule":"auth_user=g(\d+)-d(\d+) ')


def command(args, **kwargs):
    """Never include command output or arguments (possibly credentials) in errors."""
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=30, **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RuntimeError(f'emergency hy2: {Path(args[0]).name} failed') from None


def atomic(path, data):
    path = Path(path)
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.new-')
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o640)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def locked(state_dir):
    directory = Path(state_dir) / 'hy2'
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory


def read_state(directory):
    path = directory / 'generation.json'
    return json.loads(path.read_text()) if path.exists() else {'generation': -1, 'active': False}


def settings(options=None):
    result = {
        'port': 18443, 'tls_dir': '/etc/vpn2/tls/ip',
        'binary': '/usr/local/bin/sing-box', 'runtime_dir': str(MOUNT),
        'probe_url': 'https://cp.cloudflare.com/generate_204',
    }
    result.update(json.loads(OPTIONS.read_text()) if options is None else options)
    result['server_ip'] = str(ipaddress.ip_address(result['server_ip']))
    if type(result['port']) is not int or not 1024 <= result['port'] <= 65535:
        raise ValueError('emergency hy2: invalid port')
    return result


def trusted_problem(cert, key, server_ip):
    """None for a usable public IP certificate, otherwise a reason without secrets."""
    if not cert.is_file() or not key.is_file():
        return f'no certificate at {cert.parent}'
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
    except (OSError, ssl.SSLError):
        return 'certificate and key cannot be loaded as a pair'
    expiring = subprocess.run(['openssl', 'x509', '-in', str(cert), '-checkend', '3600', '-noout'],
                              capture_output=True, timeout=30)
    if expiring.returncode != 0:
        return 'certificate expired or expires within an hour'
    # Only the verify diagnostics are reported: they contain no key material.
    verified = subprocess.run(['openssl', 'verify', '-purpose', 'sslserver', '-verify_ip', server_ip,
                               '-untrusted', str(cert), str(cert)],
                              capture_output=True, text=True, timeout=30)
    if verified.returncode != 0:
        errors = [line.split('lookup:', 1)[-1].strip() for line in
                  (verified.stdout + verified.stderr).splitlines() if line.startswith('error ')]
        return 'not trusted for ' + server_ip + ': ' + (errors[0] if errors else 'verification failed')
    return None


def certificate(directory, options):
    source = Path(options['tls_dir'])
    cert, key = source / 'fullchain.pem', source / 'privkey.pem'
    problem = trusted_problem(cert, key, options['server_ip'])
    if problem:
        LOG.warning('Emergency TLS: %s; self-signed certificate, pinSHA256 in profiles', problem)
        cert, key = directory / 'fullchain.pem', directory / 'privkey.pem'
        # Reuse only a current fallback during the same issuance. A new generation
        # may replace it; previously distributed pins must never silently change.
        command(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                 'ec_paramgen_curve:P-256', '-nodes', '-days', '3650',
                 '-subj', '/CN=emergency', '-addext', f"subjectAltName=IP:{options['server_ip']}",
                 '-keyout', str(key), '-out', str(cert)])
        key.chmod(0o640)
        cert.chmod(0o640)
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        return fingerprint
    atomic(directory / 'fullchain.pem', cert.read_bytes())
    atomic(directory / 'privkey.pem', key.read_bytes())
    return None


def profile(options, password, name, pin):
    ip = ipaddress.ip_address(options['server_ip'])
    host = f'[{ip}]' if ip.version == 6 else str(ip)
    query = {'sni': str(ip)}
    if pin:
        # Hysteria checks pinSHA256 independently of CA verification. An isolated
        # insecure=1 is never emitted; the fallback must verify the exact leaf.
        query.update(insecure='1', pinSHA256=pin)
    return f"hysteria2://{quote(password, safe='')}@{host}:{options['port']}/?{urlencode(query)}#{quote(name, safe='')}"


def server_config(options, users, api_secret=None):
    directory = Path(options['runtime_dir'])
    config = {
        'log': {'level': 'warn'},
        # DNS over TLS: a node-local resolver is unreachable for the isolated user,
        # and plain UDP answers can be spoofed on path.
        'dns': {'servers': [{'type': 'tls', 'tag': 'public', 'server': '1.1.1.1'}]},
        'inbounds': [{'type': 'hysteria2', 'tag': 'emergency', 'listen': '::',
                      'listen_port': options['port'], 'users': users,
                      'tls': {'enabled': True, 'certificate_path': str(directory / 'fullchain.pem'),
                              'key_path': str(directory / 'privkey.pem')}}],
        'outbounds': [{'type': 'direct', 'tag': 'internet'}],
        'route': {'default_domain_resolver': 'public', 'rules': [
            {'ip_is_private': True, 'action': 'reject'},
            # Defence in depth; nftables (firewall_rules) is the enforcing layer.
            {'ip_cidr': [options['server_ip']], 'action': 'reject'},
        ]},
    }
    if api_secret is not None:
        # Same route as without the rule: it only labels connections per device.
        config['route']['rules'] += [
            {'auth_user': [user['name']], 'action': 'route', 'outbound': 'internet'}
            for user in users if DEVICE_USER.fullmatch(user['name'])]
        config['experimental'] = {'clash_api': {
            'external_controller': f"127.0.0.1:{options['port']}", 'secret': api_secret}}
    return config


class SystemRuntime:
    def service(self, action):
        # The agent runs with NoNewPrivileges, so no sudo: a polkit rule from
        # install-hy2.sh lets only its user start/stop/restart this one unit.
        command(['/usr/bin/systemctl', action, UNIT])

    def probe(self, state_dir):
        if os.geteuid() == 0:
            return probe(state_dir)
        # sing-box needs AF_NETLINK, which the agent sandbox does not grant;
        # the oneshot unit runs probe() and start returns its exit status.
        try:
            command(['/usr/bin/systemctl', 'start', PROBE_UNIT])
        except RuntimeError:
            return False
        return True

    def restart(self):
        self.service('restart')

    def ensure_started(self):
        self.service('start')

    def stop(self):
        self.service('stop')


def materials(directory, state):
    result = {}
    for device in state['devices']:
        uri = device['uri']
        png = command(['qrencode', '-t', 'PNG', '-o', '-', '-l', 'M'], input=uri.encode()).stdout
        path = directory / f"device-{device['id']}.txt"
        atomic(path, (uri + '\n').encode())
        result[device['id']] = {'uri': uri, 'qr_png': png, 'file': str(path)}
    return result


def authorised(devices):
    result = []
    now = datetime.now(timezone.utc)
    for device in devices:
        if device.get('allowed', True) is not True:
            continue
        expiry = device.get('expires_at')
        if expiry and datetime.fromisoformat(expiry.replace('Z', '+00:00')) <= now:
            continue
        if type(device['id']) is not int or device['id'] < 1 or not isinstance(device['name'], str):
            raise ValueError('emergency hy2: invalid device')
        result.append({'id': device['id'], 'name': device['name']})
    if len({item['id'] for item in result}) != len(result):
        raise ValueError('emergency hy2: duplicate device id')
    return sorted(result, key=lambda device: device['id'])


def start(state_dir, generation, devices, *, options=None, runtime=None):
    options = settings(options)
    runtime = runtime or SystemRuntime()
    devices = authorised(devices)
    if type(generation) is not int or generation < 0:
        raise ValueError('emergency hy2: invalid generation')
    with locked(state_dir) as directory:
        state = read_state(directory)
        if revoked_state(state, generation):
            raise ValueError('emergency hy2: generation revoked')
        # An issuance that never passed its self-test handed nothing out: the
        # retry mints fresh credentials under the same number.
        same = generation == state['generation'] and state['active']
        if same:
            previous = [{'id': item['id'], 'name': item['name']} for item in state['devices']]
            if previous != devices or state['options'] != options:
                raise ValueError('emergency hy2: changed issuance requires a new generation')
            validate(directory)
        else:
            # Durable tombstone BEFORE stopping or replacing anything: a crash
            # cannot turn old credentials into an active issuance on restart.
            atomic(directory / 'generation.json', {'generation': generation, 'active': False})
            runtime.stop()
            for path in directory.glob('device-*.txt'):
                path.unlink()
            pin = certificate(directory, options)
            users, issued = [], []
            for device in devices:
                password = secrets.token_urlsafe(32)
                users.append({'name': f"g{generation}-d{device['id']}", 'password': password})
                issued.append({**device, 'uri': profile(options, password, device['name'], pin)})
            # Readiness uses its own credential, never returned to a user.
            probe_password = secrets.token_urlsafe(32)
            users.append({'name': f'g{generation}-readiness', 'password': probe_password})
            api_secret = secrets.token_urlsafe(32)
            config = server_config(options, users, api_secret)
            atomic(directory / 'server.json', config)
            state = {'generation': generation, 'active': True, 'devices': issued,
                     'options': options, 'probe_password': probe_password, 'pin': pin,
                     'api_secret': api_secret,
                     'config_sha256': hashlib.sha256((directory / 'server.json').read_bytes()).hexdigest()}
            atomic(directory / 'generation.json', state)
        try:
            if same:
                runtime.ensure_started()
            else:
                runtime.restart()
            if not selftest(state_dir, runtime=runtime):
                raise RuntimeError('emergency hy2: real connection self-test failed')
            return materials(directory, state)
        except BaseException:
            # Credentials of a resumed generation are already with the clients:
            # those are revoked. Fresh ones were never returned, so the number
            # may be retried without a new generation and a new broadcast.
            atomic(directory / 'generation.json',
                   {'generation': generation, 'active': False, **({} if same else {'unissued': True})})
            runtime.stop()
            raise


def revoked_state(state, generation):
    return generation < state['generation'] or (
        generation == state['generation'] and not state['active'] and not state.get('unissued'))


def revoked(state_dir, generation):
    """True when this generation can never be started again: a new one is needed."""
    return revoked_state(read_state(Path(state_dir) / 'hy2'), generation)


def stop(state_dir, *, runtime=None):
    with locked(state_dir) as directory:
        state = read_state(directory)
        atomic(directory / 'generation.json', {'generation': state['generation'], 'active': False})
        (runtime or SystemRuntime()).stop()
        # Keep the tombstone even if file cleanup is interrupted.
        for path in directory.glob('device-*.txt'):
            path.unlink()
        (directory / 'server.json').unlink(missing_ok=True)


def validate(directory):
    state = read_state(directory)
    if not state['active']:
        raise ValueError('emergency hy2: inactive generation')
    digest = hashlib.sha256((directory / 'server.json').read_bytes()).hexdigest()
    if digest != state['config_sha256']:
        raise ValueError('emergency hy2: configuration does not match generation')
    return state


def refresh_tls(state_dir):
    """Renewal hook: copy the common IP certificate without changing credentials.

    Cheap enough for every agent tick: nothing happens unless the common
    certificate changed. A pinned fallback is immutable for its generation;
    changing its leaf would invalidate distributed profiles.
    """
    with locked(state_dir) as directory:
        state = read_state(directory)
        if not state['active'] or state['pin']:
            return False
        options = state['options']
        source = Path(options['tls_dir'])
        cert, key = source / 'fullchain.pem', source / 'privkey.pem'
        with contextlib.suppress(OSError):
            if cert.read_bytes() == (directory / 'fullchain.pem').read_bytes():
                return False
        problem = trusted_problem(cert, key, options['server_ip'])
        if problem:
            LOG.warning('Emergency TLS renewal skipped: %s', problem)
            return False
        # sing-box watches both paths and reloads the pair after rename.
        atomic(directory / 'privkey.pem', key.read_bytes())
        atomic(directory / 'fullchain.pem', cert.read_bytes())
        return True


def listening(port):
    """UDP socket bound to port in this network namespace (IPv4 or IPv6)."""
    suffix = f':{port:04X}'
    for table in ('/proc/net/udp', '/proc/net/udp6'):
        with contextlib.suppress(OSError):
            for line in Path(table).read_text().splitlines()[1:]:
                if line.split()[1].endswith(suffix):
                    return True
    return False


def selftest(state_dir, *, runtime=None):
    return (runtime or SystemRuntime()).probe(state_dir)


def probe(state_dir):
    """Authenticate via QUIC and fetch a public URL through the actual instance."""
    directory = Path(state_dir) / 'hy2'
    try:
        state = validate(directory)
        options = state['options']
        deadline = time.monotonic() + 15
        # A QUIC handshake sent before the server binds backs off for seconds.
        while not listening(options['port']):
            if time.monotonic() > deadline:
                return False
            time.sleep(.05)
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            proxy_port = reserved.getsockname()[1]
        config = {
            'log': {'disabled': True},
            'inbounds': [{'type': 'socks', 'listen': '127.0.0.1', 'listen_port': proxy_port}],
            'outbounds': [{'type': 'hysteria2', 'server': '127.0.0.1',
                           'server_port': options['port'], 'password': state['probe_password'],
                           'tls': {'enabled': True, 'server_name': options['server_ip'],
                                   'certificate_path': str(directory / 'fullchain.pem')}}],
        }
        with tempfile.TemporaryDirectory(dir=directory, prefix='probe-') as temporary:
            path = Path(temporary) / 'client.json'
            atomic(path, config)
            process = subprocess.Popen([options['binary'], 'run', '-c', str(path)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                while process.poll() is None and time.monotonic() < deadline:
                    response = subprocess.run([
                        'curl', '--silent', '--fail', '--noproxy', '', '--max-time', '3',
                        '--socks5-hostname', f'127.0.0.1:{proxy_port}', options['probe_url'],
                    ], capture_output=True, timeout=4)
                    if response.returncode == 0:
                        return True
                    time.sleep(.1)
            finally:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return False
    return False


def firewall_rules(uid, port, active):
    """Filter socket owner after DNS resolution, including all node-local IPs.

    Only QUIC replies may bypass destination filters. Existing outbound client
    streams never bypass them, even if conntrack already considers them valid.
    """
    if type(uid) is not int or uid < 1 or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('emergency hy2: invalid firewall identity')
    gate = '' if active else f'udp dport {port} reject'
    return f'''table inet vpn2_emergency_hy2 {{
 chain input {{ type filter hook input priority -10; policy accept;
  {gate}
 }}
 chain output {{ type filter hook output priority -10; policy accept;
  meta skuid {uid} ct direction reply udp sport {port} accept
  meta skuid {uid} ct direction reply tcp sport {port} ip daddr 127.0.0.1 accept
  meta skuid {uid} fib daddr type local reject
  meta skuid {uid} ip daddr {{ 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 }} reject
  meta skuid {uid} ip6 daddr {{ ::/96, ::ffff:0:0/96, 64:ff9b::/96, 64:ff9b:1::/48, 100::/64, 2001::/32, 2002::/16, fc00::/7, fe80::/10, ff00::/8 }} reject
 }}
}}
'''


def live_devices(state, *, timeout=3):
    """Device ids of the state's generation with a live stream right now.

    The answer is scanned in chunks: memory stays bounded with any number of
    connections. Raises OSError/HTTPException/RuntimeError when unknown.
    """
    connection = http.client.HTTPConnection('127.0.0.1', state['options']['port'], timeout=timeout)
    try:
        connection.request('GET', '/connections',
                           headers={'Authorization': 'Bearer ' + state['api_secret']})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f'emergency hy2: session API answered {response.status}')
        found, tail, deadline = set(), b'', time.monotonic() + timeout
        while chunk := response.read(65536):
            if time.monotonic() > deadline:
                raise RuntimeError('emergency hy2: session API too slow')
            data = tail + chunk
            found.update(int(match[2]) for match in SESSION_RULE.finditer(data)
                         if int(match[1]) == state['generation'])
            tail = data[-64:]
        return found
    finally:
        connection.close()


def poll_sessions(state_dir, *, fetch=live_devices, clock=time.time):
    """One observation of the current generation into hy2/sessions.json.

    Kept: devices live now and, per issuance, the flag "had a session". No
    history; a new generation starts from an empty flag set.
    """
    directory = Path(state_dir) / 'hy2'
    state = read_state(directory)
    if not state['active'] or 'api_secret' not in state:
        return None
    path = directory / 'sessions.json'
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    issued = {device['id'] for device in state['devices']}
    seen = set(previous.get('seen', [])) if previous.get('generation') == state['generation'] else set()
    try:
        live, ok = fetch(state) & issued, True
    except (OSError, ValueError, RuntimeError, http.client.HTTPException):
        live, ok = set(), False
    record = {'generation': state['generation'], 'checked_at': clock(), 'ok': ok,
              'live': sorted(live), 'seen': sorted((seen | live) & issued)}
    atomic(path, record)
    return record


def connections(state_dir, generation, *, clock=time.time):
    """{device_id: connected|seen|never} of an issuance; {} = status unknown.

    Unknown: another or inactive generation, source unreachable, or the
    observation is older than SESSIONS_STALE (poller not running).
    """
    directory = Path(state_dir) / 'hy2'
    try:
        state = read_state(directory)
        record = json.loads((directory / 'sessions.json').read_text())
    except (OSError, ValueError):
        return {}
    if (not state['active'] or state['generation'] != generation
            or record.get('generation') != generation or record.get('ok') is not True
            or not abs(clock() - record.get('checked_at', 0)) <= SESSIONS_STALE):
        return {}
    live, seen = set(record['live']), set(record['seen'])
    return {device['id']: 'connected' if device['id'] in live else
            'seen' if device['id'] in seen else 'never' for device in state['devices']}


class SessionWatch(threading.Thread):
    """Daemon poller inside the agent process; no separate service."""

    def __init__(self, state_dir, every=SESSIONS_EVERY, fetch=live_devices):
        super().__init__(name='hy2-sessions', daemon=True)
        self.state_dir, self.every, self.fetch = state_dir, every, fetch
        self.stopped = threading.Event()

    def run(self):
        while True:
            try:
                poll_sessions(self.state_dir, fetch=self.fetch)
            except Exception:  # noqa: BLE001 - the poller must outlive any bad tick
                LOG.warning('Emergency session poll failed', exc_info=True)
            if self.stopped.wait(self.every):
                return

    def stop(self, timeout=10):
        """Stop and wait for the last pass, so nothing writes into the state directory afterwards."""
        self.stopped.set()
        if self.is_alive() and self is not threading.current_thread():
            self.join(timeout)


_watches, _watches_lock = {}, threading.Lock()


def watch(state_dir, **kwargs):
    """Start (once per state directory) the session poller; idempotent."""
    key = str(Path(state_dir).resolve())
    with _watches_lock:
        current = _watches.get(key)
        # A stopped poller may still be finishing its last pass: never reuse it.
        if current is None or not current.is_alive() or current.stopped.is_set():
            current = _watches[key] = SessionWatch(state_dir, **kwargs)
            current.start()
        return current


def firewall(active):
    options = settings()
    uid = pwd.getpwnam('vpn2-hy2').pw_uid
    existing = subprocess.run(['nft', 'list', 'table', 'inet', 'vpn2_emergency_hy2'],
                              capture_output=True, timeout=10).returncode == 0
    prefix = 'delete table inet vpn2_emergency_hy2\n' if existing else ''
    command(['nft', '-f', '-'], input=(prefix + firewall_rules(uid, options['port'], active)).encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('open', 'close', 'probe'))
    args = parser.parse_args()
    if args.action == 'probe':
        raise SystemExit(0 if probe(STATE) else 1)
    if args.action == 'open':
        validate(STATE / 'hy2')
    firewall(args.action == 'open')


if __name__ == '__main__':
    main()
