"""Fixed protocol adapters. No shell, arbitrary paths, or raw diagnostics API."""

import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import socket
import subprocess
import tempfile
import time

from contract import LIMIT, require


def atomic(path, body, mode=0o600):
    descriptor, name = tempfile.mkstemp(prefix='.control-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            os.fchmod(output.fileno(), mode)
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(argv, deadline):
    """Bound both pipes while reading; never retain or publish stderr."""
    deadline = min(deadline, time.monotonic() + 10)
    output = bytearray()
    size = 0
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env={'PATH': '/usr/sbin:/usr/bin',
                                                      'LC_ALL': 'C'}) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                selector.register(process.stderr, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, 'limit')
                    for event, _mask in selector.select(remaining):
                        chunk = os.read(event.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(event.fileobj)
                            continue
                        size += len(chunk)
                        require(size <= LIMIT, 'limit')
                        if event.fileobj is process.stdout:
                            output.extend(chunk)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            require(process.returncode == 0, 'unavailable')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return output.decode('utf-8', errors='strict')


def peer_addresses(peer):
    # Keep the existing client profiles byte-compatible, including the legacy
    # /24 mapping. Decimal octet strings intentionally occupy hexadecimal ULA
    # fields, exactly as in the current panel's wgUlaAddress.
    subnet, host = peer['ip'].split('.')[2:]
    suffix = host if subnet == '66' else subnet + ':' + host
    return [peer['ip'] + '/32', 'fd66:66::' + suffix + '/128']


class Backend:
    def __init__(self, root=Path('/'), state=Path('/var/lib/vpn2-control')):
        self.root = root
        self.state = state
        self.deadline = 0

    def path(self, name):
        return self.root / name.lstrip('/')

    def command(self, *args):
        return run(list(args), self.deadline)

    def version(self, domain, params):
        path = self.state / (domain + '.json')
        digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        if path.exists():
            previous = json.loads(path.read_text())
            require(params['version'] >= previous['version'], 'stale')
            if params['version'] == previous['version']:
                require(digest == previous['digest'], 'conflict')
        atomic(path, json.dumps({'version': params['version'], 'digest': digest}))

    def execute(self, method, params):
        self.deadline = time.monotonic() + 30
        if method == 'state.read':
            return self.observe(params['protocol'])
        if method == 'check.run':
            if params['check'] == 'readiness':
                return {'ready': True, 'api': 1}
            return self.observe(params['check'])
        protocol = method.split('.')[0]
        domain = protocol
        if protocol == 'openvpn':
            domain += '-' + str(params['device_id'])
        self.version(domain, params)
        if protocol == 'wg':
            result = self.wireguard(params)
        elif protocol == 'openvpn':
            result = self.openvpn(params)
        else:
            result = self.ikev2(params)
        return {'version': params['version'], 'observed': result}

    def wireguard(self, params):
        path = self.path('/etc/wireguard/wg0.conf')
        # Preserve deployment-owned interface configuration (addresses, key,
        # port). Only peers are an API resource; no input becomes a directive.
        previous = path.read_text()
        header = re.split(r'(?m)^\s*\[Peer\]\s*$', previous)[0].rstrip() + '\n'
        require('[Interface]' in header, 'unavailable')
        peers = ''.join('\n[Peer]\nPublicKey = ' + peer['key'] +
                        '\nPresharedKey = ' + peer['preshared_key'] +
                        '\nAllowedIPs = ' + ', '.join(peer_addresses(peer)) + '\n'
                        for peer in params['peers'])
        # wg-quick is deliberately not executed: it is a shell script with
        # hook directives. syncconf receives only the typed WG subset.
        interface = []
        for line in header.splitlines():
            if re.match(r'^\s*(PrivateKey|ListenPort|FwMark)\s*=', line):
                interface.append(line)
        require(any(re.match(r'^\s*PrivateKey\s*=', line) for line in interface),
                'unavailable')
        with tempfile.TemporaryDirectory(dir=self.state) as work:
            candidate = Path(work) / 'wg.conf'
            atomic(candidate, '[Interface]\n' + '\n'.join(interface) + '\n' + peers)
            self.command('/usr/bin/wg', 'syncconf', 'wg0', str(candidate))
        result = self.observe('wg')
        expected = {peer['key']: peer_addresses(peer) for peer in params['peers']}
        require(result['peers'] == expected, 'unavailable')
        loaded_psks = dict(line.split('\t', 1) for line in self.command(
            '/usr/bin/wg', 'show', 'wg0', 'preshared-keys').splitlines())
        require(loaded_psks == {peer['key']: peer['preshared_key'] for peer in params['peers']},
                'unavailable')
        atomic(path, header + peers)
        return result

    def management(self, command):
        deadline = min(self.deadline, time.monotonic() + 5)
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.connect(str(self.path('/etc/openvpn/server/mgmt.sock')))
            connection.sendall((command + '\n').encode())
            output = bytearray()
            while True:
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                require(time.monotonic() < deadline, 'limit')
                chunk = connection.recv(8192)
                require(bool(chunk), 'unavailable')
                output.extend(chunk)
                require(len(output) <= LIMIT, 'limit')
                lines = output.decode().splitlines()
                # Do not treat a partial last line as a complete reply.
                if not output.endswith(b'\n'):
                    lines = lines[:-1]
                if any(line == 'END' or line.startswith(('SUCCESS:', 'ERROR:'))
                       for line in lines):
                    connection.sendall(b'quit\n')
                    return lines

    def certificates(self, device_id):
        cn = 'dev_' + str(device_id)
        matches = []
        for line in self.path('/etc/openvpn/server/pki/index.txt').read_text().splitlines():
            columns = line.split('\t')
            if len(columns) == 6 and re.search(r'(?:^|/)CN=' + cn + r'(?:/|$)', columns[5]):
                require(re.fullmatch(r'[A-Fa-f0-9]{1,64}', columns[3]), 'unavailable')
                matches.append((columns[0], columns[3]))
        return matches

    def openvpn(self, params):
        base = self.path('/etc/openvpn/server')
        cn = 'dev_' + str(params['device_id'])
        # Same lock as deployment/legacy issuance; no overlapping CA changes.
        with (base / '.pki.lock').open('a') as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < self.deadline, 'limit')
                    time.sleep(0.02)
            certs = self.certificates(params['device_id'])
            require(bool(certs), 'unavailable')
            allowed = params['action'] == 'allow'
            if allowed:
                require(any(status == 'V' for status, _serial in certs), 'conflict')
            mask = '255.255.255.0' if params['ip'].split('.')[2] == '67' else '255.255.248.0'
            body = f"ifconfig-push {params['ip']} {mask}\n"
            if not allowed:
                body += 'disable\n'
            atomic(base / 'ccd' / cn, body, 0o644)
            if params['action'] == 'revoke':
                for status, serial in certs:
                    if status == 'V':
                        self.command('/usr/bin/openssl', 'ca', '-batch', '-config',
                                     str(base / 'pki/openssl.cnf'), '-revoke',
                                     str(base / 'pki/newcerts' / (serial + '.pem')))
                with tempfile.TemporaryDirectory(dir=base / 'pki') as work:
                    crl = Path(work) / 'crl.pem'
                    self.command('/usr/bin/openssl', 'ca', '-batch', '-config',
                                 str(base / 'pki/openssl.cnf'), '-gencrl', '-out', str(crl))
                    self.command('/usr/bin/openssl', 'crl', '-in', str(crl), '-noout',
                                 '-verify', '-CAfile', str(base / 'pki/ca.crt'))
                    atomic(base / 'pki/crl.pem', crl.read_text(), 0o644)
                require(all(status != 'V' for status, _serial in
                            self.certificates(params['device_id'])), 'unavailable')
            current = self.observe('openvpn')
            # Address changes also need reconnecting the matching sessions.
            if cn in current['sessions'] and (not allowed or
                                             current['sessions'][cn] != params['ip']):
                self.management('kill ' + cn)
                current = self.observe('openvpn')
                require(cn not in current['sessions'], 'unavailable')
            return {'device_id': params['device_id'], 'allowed': allowed,
                    'sessions': int(cn in current['sessions']),
                    'revoked': all(status != 'V' for status, _serial in
                                   self.certificates(params['device_id']))}

    def sas(self):
        text = self.command('/usr/sbin/swanctl', '--list-sas')
        result = []
        current = None
        for line in text.splitlines():
            match = re.match(r'^\S+:\s+#(\d+),', line)
            if match:
                current = int(match[1])
            identity = re.search(r"EAP:\s+'([^']+)'", line)
            if identity and current:
                result.append({'id': current, 'username': identity[1]})
                current = None
        return result

    def ikev2(self, params):
        path = self.path('/etc/swanctl/conf.d/anysda-clients.conf')
        clients = params['clients']
        # Persist identities requiring termination BEFORE changing files. This
        # survives death after load or terminate and is cleared only on proof.
        pending_path = self.state / 'ikev2-pending.json'
        pending = set(json.loads(pending_path.read_text())) if pending_path.exists() else set()
        previous_path = self.state / 'ikev2-identities.json'
        previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
        current = {client['username']: hashlib.sha256(json.dumps(client, sort_keys=True)
                                                     .encode()).hexdigest() for client in clients}
        pending.update(name for name, digest in previous.items() if current.get(name) != digest)
        # First migration cannot establish old credential equality, so terminate
        # existing SAs once. Later retries and no-op applies keep valid sessions.
        if not previous_path.exists():
            pending.update(sa['username'] for sa in self.sas())
        pending.update(sa['username'] for sa in self.sas() if sa['username'] not in current)
        atomic(pending_path, json.dumps(sorted(pending)))
        lines = ['connections {', '  anysda-ikev2 { remote-client { groups = vpn2-no-access } }']
        for client in clients:
            name = 'anysda-dev-' + str(client['device_id'])
            lines += [f'  {name} : connections.anysda-ikev2 {{',
                      f'    pools = {name}, anysda-ikev2-pool6',
                      '    remote-client {', f'      eap_id = "{client["username"]}"',
                      '      groups =', '    }', '  }']
        lines += ['}', 'pools {']
        for client in clients:
            name = 'anysda-dev-' + str(client['device_id'])
            lines += [f'  {name} : pools.anysda-ikev2-pool {{',
                      f'    addrs = {client["ip"]}-{client["ip"]}', '  }']
        lines += ['}', 'secrets {']
        for client in clients:
            lines += [f'  eap-{client["device_id"]} {{',
                      f'    id = "{client["username"]}"',
                      f'    secret = "{client["password"]}"', '  }']
        lines += ['}', '']
        atomic(path, '\n'.join(lines))
        self.command('/usr/sbin/swanctl', '--load-creds', '--clear', '--noprompt')
        self.command('/usr/sbin/swanctl', '--load-pools')
        self.command('/usr/sbin/swanctl', '--load-conns')
        for sa in self.sas():
            if sa['username'] in pending:
                self.command('/usr/sbin/swanctl', '--terminate', '--ike-id', str(sa['id']))
        observed = self.observe('ikev2')
        require(not any(sa['username'] in pending for sa in observed['sas']), 'unavailable')
        require(set(observed['devices']) == {client['device_id'] for client in clients},
                'unavailable')
        atomic(previous_path, json.dumps(current))
        atomic(pending_path, '[]')
        return observed

    def observe(self, protocol):
        if protocol == 'wg':
            peers = {}
            for line in self.command('/usr/bin/wg', 'show', 'wg0', 'allowed-ips').splitlines():
                public_key, addresses = line.split('\t', 1)
                peers[public_key] = sorted(addresses.replace(',', ' ').split())
            return {'peers': peers}
        if protocol == 'openvpn':
            rows = list(csv.reader(self.management('status 2')))
            require(any(row == ['END'] for row in rows), 'unavailable')
            sessions = {row[1]: row[3] for row in rows if len(row) >= 4 and
                        row[0] == 'CLIENT_LIST' and re.fullmatch(r'dev_[1-9][0-9]*', row[1])}
            return {'sessions': sessions}
        conns = self.command('/usr/sbin/swanctl', '--list-conns')
        devices = [int(value) for value in re.findall(r'^anysda-dev-(\d+):', conns, re.M)]
        return {'devices': devices, 'sas': self.sas()}
