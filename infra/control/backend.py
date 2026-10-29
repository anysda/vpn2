"""Fixed protocol adapters. No shell, arbitrary paths, or raw diagnostics API."""

import csv
import base64
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

from contract import API, Fault, LIMIT, METHODS, key as validate_key, require

# C isspace() within a line, as openvpn and wg read their configs; not Python's \s.
BLANK = r'[ \t\v\f\r]'


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


def run(argv, deadline, data=None):
    """Bound both pipes while reading; never retain or publish stderr."""
    deadline = min(deadline, time.monotonic() + 10)
    output = bytearray()
    size = 0
    pending = memoryview(data.encode() if data is not None else b'')
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env={'PATH': '/usr/sbin:/usr/bin',
                                                      'LC_ALL': 'C'}) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                selector.register(process.stderr, selectors.EVENT_READ)
                if process.stdin:
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, 'limit')
                    for event, _mask in selector.select(remaining):
                        if event.fileobj is process.stdin:
                            try:
                                pending = pending[os.write(process.stdin.fileno(), pending[:8192]):]
                            except BrokenPipeError:
                                pending = pending[:0]
                            if not pending:
                                selector.unregister(process.stdin)
                                process.stdin.close()
                            continue
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


def utc_now():
    # The panel stores expiry as UTC milliseconds and sends toISOString(), so
    # the same fixed-width form compares as text.
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())


def expired(item, now):
    return item.get('expires_at') is not None and item['expires_at'] <= now


def strip(item):
    return {name: value for name, value in item.items() if name != 'expires_at'}


def peer_addresses(peer):
    # Keep the existing client profiles byte-compatible, including the legacy
    # /24 mapping. Decimal octet strings intentionally occupy hexadecimal ULA
    # fields, exactly as in the current panel's wgUlaAddress.
    subnet, host = peer['ip'].split('.')[2:]
    suffix = host if subnet == '66' else subnet + ':' + host
    return [peer['ip'] + '/32', 'fd66:66::' + suffix + '/128']


MAX_JOURNAL = 2000


class Backend:
    def __init__(self, root=Path('/'), state=Path('/var/lib/vpn2-control'), pools=None):
        self.root = root
        self.state = state
        self.deadline = 0
        self.pools = pools

    def path(self, name):
        return self.root / name.lstrip('/')

    def command(self, *args, data=None):
        return run(list(args), self.deadline, data)

    def version(self, domain, params):
        path = self.state / (domain + '.json')
        digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        if path.exists():
            previous = json.loads(path.read_text())
            require(params['version'] >= previous['version'], 'stale')
            if params['version'] == previous['version']:
                require(digest == previous['digest'], 'conflict')
        # The applied state stays here for the expiry timer, which must work
        # while the panel is stopped.
        atomic(path, json.dumps({'version': params['version'], 'digest': digest, 'params': params}))

    def applied(self, domain):
        path = self.state / (domain + '.json')
        return json.loads(path.read_text()).get('params') if path.exists() else None

    def expires(self, protocol):
        if protocol == 'openvpn':
            result = {}
            for path in self.state.glob('openvpn-*.json'):
                params = json.loads(path.read_text()).get('params') or {}
                if params.get('expires_at'):
                    result[str(params['device_id'])] = params['expires_at']
            return result
        params = self.applied(protocol) or {}
        if protocol == 'wg':
            return {peer['key']: peer['expires_at'] for peer in params.get('peers', []) if peer.get('expires_at')}
        return {str(client['device_id']): client['expires_at'] for client in params.get('clients', [])
                if client.get('expires_at')}

    def unexpired(self, protocol, params, now):
        # A replay or a late apply must not return access that has expired.
        if protocol == 'openvpn':
            if expired(params, now) and params['action'] == 'allow':
                params = dict(params, action='deny')
            return strip(params)
        name = 'peers' if protocol == 'wg' else 'clients'
        return dict(params, **{name: [strip(item) for item in params[name] if not expired(item, now)]})

    def expire(self):
        """Take expired devices off with the same paths as an apply."""
        now = utc_now()
        journal_path = self.state / 'expired.json'
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else []
        removed = []
        failed = []
        for protocol in ('wg', 'ikev2', 'openvpn'):
            try:
                removed += self.expire_protocol(protocol, now)
            except (Fault, OSError, KeyError, ValueError, subprocess.TimeoutExpired):
                # One protocol down must not keep the others' access open.
                failed.append(protocol)
        if removed:
            journal = (journal + [dict(item, at=now) for item in removed])[-MAX_JOURNAL:]
            atomic(journal_path, json.dumps(journal))
        return {'removed': journal, 'latest': removed, 'failed': failed}

    def expire_protocol(self, protocol, now):
        if protocol == 'openvpn':
            removed = []
            for path in sorted(self.state.glob('openvpn-*.json')):
                params = json.loads(path.read_text()).get('params')
                if not params or params['action'] != 'allow' or not expired(params, now):
                    continue
                cn = 'dev_' + str(params['device_id'])
                ccd = self.path('/etc/openvpn/server/ccd') / cn
                text = ccd.read_text() if ccd.exists() else ''
                disabled = bool(re.search(rf'(?m)^{BLANK}*disable{BLANK}*$', text))
                if disabled and cn not in self.observe('openvpn')['sessions']:
                    continue
                self.openvpn(dict(strip(params), action='deny'))
                # A client retrying after the drop is cut again, not reported again.
                if not disabled:
                    removed.append({'protocol': protocol, 'id': params['device_id'], 'expires_at': params['expires_at']})
            return removed
        params = self.applied(protocol)
        name = 'peers' if protocol == 'wg' else 'clients'
        gone = [item for item in (params or {}).get(name, []) if expired(item, now)]
        if not gone:
            return []
        observed = self.observe(protocol)
        if protocol == 'wg':
            configured = [item for item in gone if item['key'] in observed['peers']]
            live = configured
        else:
            usernames = {sa['username'] for sa in observed['sas']}
            configured = [item for item in gone if item['device_id'] in observed['devices']]
            live = configured + [item for item in gone if item not in configured and item['username'] in usernames]
        if not live:
            return []
        rest = self.unexpired(protocol, params, now)
        if protocol == 'wg':
            self.wireguard(rest)
        else:
            self.ikev2(rest)
        # A client retrying after the drop is cut again, not reported again.
        return [{'protocol': protocol, 'id': item['key'] if protocol == 'wg' else item['device_id'],
                 'expires_at': item['expires_at']} for item in configured]

    def execute(self, method, params):
        self.deadline = time.monotonic() + 30
        if method == 'api.describe':
            return {'api': API, 'methods': list(METHODS)}
        if method == 'routes.plan':
            # A fixed public resource may exceed the per-response limit.
            # Open once per chunk; an atomic router replacement changes revision.
            with self.path('/etc/anysda/route-plan.json').open('rb') as source:
                stat = os.fstat(source.fileno())
                require(stat.st_size <= 32 * LIMIT, 'limit')
                revision = f'{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}'
                require(not params['revision'] or params['revision'] == revision, 'stale')
                source.seek(params['offset'])
                data = source.read(LIMIT // 4)
                return {'revision': revision, 'size': stat.st_size, 'data': base64.b64encode(data).decode()}
        if method == 'profile.read':
            return self.profile(params['protocol'])
        if method == 'traffic.read':
            return self.traffic(params['protocol'])
        if method in ('settings.read', 'settings.apply'):
            paths = {'routes': 'manual-routes.json', 'preferences': 'client-prefs.json',
                     'disabled-exits': 'exits-disabled.json'}
            path = self.path('/etc/anysda/' + paths[params['resource']])
            if method == 'settings.apply':
                self.version('settings-' + params['resource'], params)
                body = json.dumps(params['config'], sort_keys=True) + '\n'
                if not path.exists() or path.read_text() != body:
                    atomic(path, body, 0o644)
            default = {'disabled': []} if params['resource'] == 'disabled-exits' else {}
            return {'config': json.loads(path.read_text()) if path.exists() else default}
        if method in ('openvpn.issue', 'openvpn.revoke', 'openvpn.certificate', 'openvpn.disconnect'):
            return self.openvpn_resource(method, params)
        if method == 'state.read':
            return dict(self.observe(params['protocol']), expires=self.expires(params['protocol']))
        if method == 'expiry.run':
            return self.expire()
        if method == 'check.run':
            if params['check'] == 'readiness':
                return {'ready': True, 'api': 1}
            return self.observe(params['check'])
        protocol = method.split('.')[0]
        domain = protocol
        if protocol == 'openvpn':
            domain += '-' + str(params['device_id'])
        self.version(domain, params)
        params = self.unexpired(protocol, params, utc_now())
        if protocol == 'wg':
            result = self.wireguard(params)
        elif protocol == 'openvpn':
            result = self.openvpn(params)
        else:
            result = self.ikev2(params)
        return {'version': params['version'], 'observed': result}

    def profile(self, protocol):
        if protocol == 'wg':
            return {'publicKey': self.path('/etc/wireguard/server.pub').read_text().strip()}
        if protocol == 'openvpn':
            base = self.path('/etc/openvpn/server/pki')
            ready = all((base / name).is_file() for name in ('ca.crt', 'ca.key', 'openssl.cnf'))
            return {'ready': ready, 'ca': (base / 'ca.crt').read_text() if ready else '',
                    'tlsCrypt': (base / 'tls-crypt.key').read_text() if ready else ''}
        def marker(name):
            path = self.path('/etc/anysda/ikev2-' + name)
            return path.read_text().strip() if path.exists() else ''
        ca = self.path('/etc/strongswan/pki/ca.crt')
        return {'ready': bool(marker('mode') or ca.exists()), 'mode': marker('mode') or 'self-signed',
                'host': marker('server-host'), 'domain': marker('domain'),
                'ca': ca.read_text() if ca.exists() else ''}

    def pki_lock(self, lock):
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                require(time.monotonic() < self.deadline, 'limit')
                time.sleep(0.02)

    def served(self, path):
        # OpenVPN re-reads the CRL and ccd after dropping to user nobody and
        # group nogroup, so only the "other" bits apply. An unreadable CRL
        # keeps the old one in memory, an unreadable ccd ignores "disable",
        # and neither is an error the server reports.
        try:
            if not path.stat().st_mode & 0o004:
                return False
            for parent in path.parents:
                if parent == self.root != Path('/'):
                    break
                if not parent.stat().st_mode & 0o001:
                    return False
        except FileNotFoundError:
            return False
        return True

    def crl(self, base):
        target = base / 'pki/crl.pem'
        config = base / 'server.conf'
        if config.exists():
            listed = re.search(rf'(?m)^{BLANK}*crl-verify{BLANK}+([^ \t\n\v\f\r]+){BLANK}*$', config.read_text())
            require(bool(listed) and self.path(listed[1]) == target, 'unavailable')
        before = target.stat() if target.exists() else None
        with tempfile.TemporaryDirectory(dir=base / 'pki') as work:
            crl = Path(work) / 'crl.pem'
            self.command('/usr/bin/openssl', 'ca', '-batch', '-config',
                         str(base / 'pki/openssl.cnf'), '-gencrl', '-out', str(crl))
            self.command('/usr/bin/openssl', 'crl', '-in', str(crl), '-noout',
                         '-verify', '-CAfile', str(base / 'pki/ca.crt'))
            atomic(target, crl.read_text(), 0o644)
        # OpenVPN reloads the CRL only when mtime (whole seconds) or size
        # changed since its last load; a same-size rewrite in the same second
        # would be skipped until the next change.
        after = target.stat()
        if before and int(after.st_mtime) == int(before.st_mtime) and after.st_size == before.st_size:
            os.utime(target, (after.st_atime, int(before.st_mtime) + 1))
        require(self.served(target), 'unavailable')

    def openvpn_resource(self, method, params):
        base = self.path('/etc/openvpn/server')
        device_id = params['device_id']
        cn = 'dev_' + str(device_id)
        if method == 'openvpn.disconnect':
            present = cn in self.observe('openvpn')['sessions']
            if present:
                self.management('kill ' + cn)
            require(cn not in self.observe('openvpn')['sessions'], 'unavailable')
            return {'killed': present}
        with (base / '.pki.lock').open('a') as lock:
            self.pki_lock(lock)
            if method == 'openvpn.issue':
                # The journal retains the same key/CSR across a crash. A crash
                # after signing is recovered from newcerts by public key.
                directory = self.state / ('issue-' + str(device_id) + '-' + params['generation'])
                directory.mkdir(mode=0o700, exist_ok=True)
                key, csr, cert = (directory / name for name in ('key.pem', 'csr.pem', 'cert.pem'))
                if not key.exists():
                    candidate = directory / 'key.tmp'
                    self.command('/usr/bin/openssl', 'genpkey', '-algorithm', 'EC',
                                 '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-out', str(candidate))
                    atomic(key, candidate.read_text())
                    candidate.unlink()
                if not cert.exists():
                    public = self.command('/usr/bin/openssl', 'pkey', '-in', str(key), '-pubout')
                    for status, serial in self.certificates(device_id):
                        existing = base / 'pki/newcerts' / (serial + '.pem')
                        if status == 'V' and self.command('/usr/bin/openssl', 'x509', '-in', str(existing),
                                                          '-noout', '-pubkey') == public:
                            atomic(cert, existing.read_text())
                            break
                if not cert.exists():
                    self.command('/usr/bin/openssl', 'req', '-new', '-key', str(key),
                                 '-out', str(csr), '-subj', '/CN=' + cn)
                    candidate = directory / 'cert.tmp'
                    self.command('/usr/bin/openssl', 'ca', '-batch', '-notext', '-config',
                                 str(base / 'pki/openssl.cnf'), '-extensions', 'client_ext',
                                 '-days', '3650', '-in', str(csr), '-out', str(candidate))
                    atomic(cert, candidate.read_text())
                    candidate.unlink()
                serial = self.command('/usr/bin/openssl', 'x509', '-in', str(cert), '-noout', '-serial')
                require(('V', serial.strip().split('=', 1)[1]) in self.certificates(device_id), 'conflict')
                return {'cert': cert.read_text(), 'key': key.read_text()}
            matches = [(status, serial) for status, serial in self.certificates(device_id)
                       if int(serial, 16) == int(params['serial'], 16)]
            require(len(matches) == 1, 'validation')
            status, serial = matches[0]
            cert = base / 'pki/newcerts' / (serial + '.pem')
            if method == 'openvpn.revoke':
                if status != 'R':
                    self.command('/usr/bin/openssl', 'ca', '-batch', '-config',
                                 str(base / 'pki/openssl.cnf'), '-revoke', str(cert))
                self.crl(base)
                return {'revoked': True}
            # openssl verify uses the published CRL, not just the CA index.
            from contract import Fault
            valid = False
            try:
                self.command('/usr/bin/openssl', 'verify', '-CAfile', str(base / 'pki/ca.crt'),
                             '-CRLfile', str(base / 'pki/crl.pem'), '-crl_check', str(cert))
                valid = True
            except Fault:
                pass
            revoked = False
            if status == 'R' and self.served(base / 'pki/crl.pem'):
                crl = self.command('/usr/bin/openssl', 'crl', '-in', str(base / 'pki/crl.pem'), '-text', '-noout')
                revoked = any(int(value, 16) == int(serial, 16) for value in
                              re.findall(r'Serial Number:\s*([0-9A-Fa-f]+)', crl))
            ccd = base / 'ccd' / cn
            text = ccd.read_text() if ccd.exists() else None
            # A ccd the server cannot read is the same as none: it connects.
            denied = text is not None and self.served(ccd) and re.search(rf'(?m)^{BLANK}*disable{BLANK}*$', text)
            return {'valid': valid, 'revoked': revoked, 'present': text is not None,
                    'allowed': text is not None and not denied}

    def traffic(self, protocol):
        if protocol == 'wg':
            rows = []
            for line in self.command('/usr/bin/wg', 'show', 'wg0', 'transfer').splitlines():
                key, rx, tx = line.split('\t')
                rows.append({'key': key, 'rx': int(tx), 'tx': int(rx)})
            return {'samples': rows}
        if protocol == 'openvpn':
            rows = []
            for row in csv.reader(self.management('status 2')):
                if len(row) >= 7 and row[0] == 'CLIENT_LIST' and re.fullmatch(r'dev_[1-9][0-9]*', row[1]):
                    rows.append({'device_id': int(row[1][4:]), 'rx': int(row[6]), 'tx': int(row[5])})
            return {'samples': rows}
        # The existing host sampler is read-only after the API migration.
        data = json.loads(self.path('/etc/anysda/ikev2-status.json').read_text())
        require(time.time() - data['updated'] < 30, 'unavailable')
        return {'samples': [{name: item[name] for name in ('uniqueid', 'username', 'rx', 'tx')}
                            for item in data['sessions']]}

    def wireguard(self, params):
        path = self.path('/etc/wireguard/wg0.conf')
        # Preserve deployment-owned interface configuration (addresses, key,
        # port). Only peers are an API resource; no input becomes a directive.
        previous = path.read_text()
        header = re.split(rf'(?m)^[ \t\n\v\f\r]*\[Peer\]{BLANK}*$', previous)[0].rstrip(' \t\n\v\f\r') + '\n'
        require('[Interface]' in header, 'unavailable')
        # The persisted key pair is authoritative, as in the former panel.
        # A keys-only restore can leave this host's old rendered wg0.conf.
        # Never regenerate a key or return it to the API caller.
        private = self.path('/etc/wireguard/server.priv')
        if private.exists():
            canonical = private.read_text().strip()
            validate_key(canonical)
            header = re.sub(rf'(?m)^{BLANK}*PrivateKey{BLANK}*=.*$', 'PrivateKey = ' + canonical, header)
        peers = ''.join('\n[Peer]\nPublicKey = ' + peer['key'] +
                        '\nPresharedKey = ' + peer['preshared_key'] +
                        '\nAllowedIPs = ' + ', '.join(peer_addresses(peer)) + '\n'
                        for peer in params['peers'])
        # wg-quick is deliberately not executed: it is a shell script with
        # hook directives. syncconf receives only the typed WG subset.
        interface = []
        for line in header.split('\n'):  # wg ends a line only at a newline
            if re.match(rf'{BLANK}*(PrivateKey|ListenPort|FwMark){BLANK}*=', line):
                interface.append(line)
        require(any(re.match(rf'{BLANK}*PrivateKey{BLANK}*=', line) for line in interface),
                'unavailable')
        # Through stdin: the distribution's AppArmor profile for wg lets it
        # open only /etc/wireguard, which it cannot traverse without root.
        self.command('/usr/bin/wg', 'syncconf', 'wg0', '/dev/stdin',
                     data='[Interface]\n' + '\n'.join(interface) + '\n' + peers)
        result = self.observe('wg')
        expected = {peer['key']: peer_addresses(peer) for peer in params['peers']}
        require(result['peers'] == expected, 'unavailable')
        loaded_psks = dict(line.split('\t', 1) for line in self.command(
            '/usr/bin/wg', 'show', 'wg0', 'preshared-keys').splitlines())
        require(loaded_psks == {peer['key']: peer['preshared_key'] for peer in params['peers']},
                'unavailable')
        atomic(path, header + peers)
        self.command('/usr/sbin/ip', '-6', 'addr', 'replace', 'fd66:66::1/64', 'dev', 'wg0')
        from contract import load_pools
        for subnet in (self.pools or load_pools())['wg']['subnets']:
            self.command('/usr/sbin/ip', 'route', 'replace', subnet, 'dev', 'wg0')
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
            require(self.served(base / 'ccd' / cn), 'unavailable')
            if params['action'] == 'revoke':
                for status, serial in certs:
                    if status == 'V':
                        self.command('/usr/bin/openssl', 'ca', '-batch', '-config',
                                     str(base / 'pki/openssl.cnf'), '-revoke',
                                     str(base / 'pki/newcerts' / (serial + '.pem')))
                self.crl(base)
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
            # swanctl печатает EAP-логин, только если он отличается от IKE-идентификатора.
            identity = re.search(r"EAP:\s+'([^']+)'", line) or re.match(r"^\s+remote\s+'([^']+)'", line)
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
        current = {client['username']: hashlib.sha256(json.dumps(client, sort_keys=True, separators=(',', ':'))
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
                      f'    addrs = {client["ip"]}-{client["ip"]}',
                      '    dns = ' + ', '.join(client.get('dns', ['10.99.0.1'])), '  }']
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
        atomic(self.state / 'ikev2-config-digest', hashlib.sha256(path.read_bytes()).hexdigest())
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
        credentials = {}
        saved = self.state / 'ikev2-identities.json'
        digest = self.state / 'ikev2-config-digest'
        config = self.path('/etc/swanctl/conf.d/anysda-clients.conf')
        if saved.exists() and digest.exists() and config.exists() and (
                digest.read_text() == hashlib.sha256(config.read_bytes()).hexdigest()):
            credentials = json.loads(saved.read_text())
        return {'devices': devices, 'sas': self.sas(), 'credentials': credentials}
