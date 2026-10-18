#!/usr/bin/env python3
"""Let's Encrypt lifecycle on top of Caddy, shared by the entry and exits.

Caddy obtains and renews certificates (HTTP-01 on TCP 80; HTTP/3 stays off).
This tool, run once a minute by vpn2-tls.timer:
  * pins the renewal window (IP: 5 days after issuance, domain: 7 days
    before expiry) from the lifetime of the certificate actually issued;
  * exports the current pair to <output_dir>/{fullchain,privkey}.pem;
  * panel role: switches HTTP to HTTPS (redirect, Secure cookie, CSRF origin)
    after the first certificate;
  * queues an admin alert on an ACME failure (error, expiry of the current
    certificate; at most once per notify_hours) and "updated" after recovery.
Only the Python standard library and the openssl CLI are used.
"""

import argparse
import fcntl
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import time
import urllib.request
import uuid

LE = 'https://acme-v02.api.letsencrypt.org/directory'
DAY = 86400
# Let's Encrypt "shortlived" profile: 160 hours.
SHORTLIVED = 160 * 3600
IP_RENEW_AFTER = 5 * DAY
DOMAIN_RENEW_BEFORE = 7 * DAY


def atomic(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.new')
    with temporary.open('w') as stream:
        os.chmod(temporary, mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def read_text(path, default=''):
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return default


def is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def authority(host):
    if is_ip(host):
        return f'[{host}]' if ':' in host else host
    if not re.fullmatch(r'[a-zA-Z0-9.-]+', host):
        raise ValueError('invalid certificate host')
    return host


def tls_config(config, lifetime=None):
    """Caddy `tls` block. Caddy only knows a ratio of the lifetime, so the
    window is recomputed from the issued certificate. ARI from the CA can
    still move a renewal earlier (Caddy 2.11 has no switch to ignore it)."""
    ip = is_ip(config['host'])
    lifetime = lifetime or (SHORTLIVED if ip else 90 * DAY)
    remaining = max(3600, lifetime - IP_RENEW_AFTER) if ip else DOMAIN_RENEW_BEFORE
    ratio = min(.95, remaining / lifetime)
    ca = config.get('acme_url', LE)
    if not re.fullmatch(r'https?://[^\s{}"\\]+', ca):
        raise ValueError('invalid ACME URL')
    root = config.get('acme_root', '')
    trust = f'\n        trusted_roots {json.dumps(root)}' if root else ''
    profile = '\n        profile shortlived' if ip else ''
    return (f'tls {{\n    renewal_window_ratio {ratio:.9f}\n'
            f'    issuer acme {{\n        dir {ca}{profile}{trust}\n        timeout 60s\n'
            '        disable_tlsalpn_challenge\n    }\n}\n')


def exit_caddyfile(config):
    """Exit node: Caddy is only an ACME client. HTTP-01 on :80, the managed
    site listens on loopback so TCP 443 stays free for the emergency panel."""
    caddy_dir = config['caddy_dir']
    return ('{\n    auto_https disable_redirects\n'
            f"    default_sni {config['host']}\n"
            '    servers {\n        protocols h1 h2\n    }\n}\n'
            f"https://{authority(config['host'])}:{config.get('local_port', 8443)} {{\n"
            '    bind 127.0.0.1\n'
            f'    import {caddy_dir}/vpn2-tls.conf\n'
            '    respond 404\n}\n'
            ':80 {\n    respond 404\n}\n')


def certificate(cert, key, host, now):
    """Verify the SAN, validity and key pair before exporting Caddy storage."""
    info = ssl._ssl._test_decode_cert(str(cert))
    start = ssl.cert_time_to_seconds(info['notBefore'])
    end = ssl.cert_time_to_seconds(info['notAfter'])
    sans = info.get('subjectAltName', ())
    if is_ip(host):
        matches = any(kind == 'IP Address' and ipaddress.ip_address(value) == ipaddress.ip_address(host)
                      for kind, value in sans)
    else:
        matches = ('DNS', host) in sans
    if not matches or not start <= now < end:
        return None
    cert_pub = subprocess.check_output(['openssl', 'x509', '-in', str(cert), '-pubkey', '-noout'],
                                       stderr=subprocess.DEVNULL, timeout=5)
    key_pub = subprocess.check_output(['openssl', 'pkey', '-in', str(key), '-pubout'],
                                      stderr=subprocess.DEVNULL, timeout=5)
    if cert_pub != key_pub:
        return None
    return {'fingerprint': hashlib.sha256(cert.read_bytes()).hexdigest(),
            'start': start, 'end': end, 'cert': str(cert), 'key': str(key)}


def current_certificate(config, now):
    candidates = []
    # Caddy storage sanitizes IPv6 colons into underscores.
    name = config['host'].lower().replace(':', '_')
    for cert in Path(config['storage']).glob(f'certificates/*/{name}/{name}.crt'):
        try:
            metadata = read_json(cert.with_suffix('.json'), {})
            if metadata.get('issuer_data', {}).get('ca') != config.get('acme_url', LE):
                continue
            item = certificate(cert, cert.with_suffix('.key'), config['host'], now)
            if item:
                candidates.append(item)
        except (OSError, ValueError, ssl.SSLError, subprocess.SubprocessError):
            continue
    return max(candidates, key=lambda item: item['end'], default=None)


def export_certificate(config, cert):
    """One symlink switch publishes the pair; the previous generation is kept
    for a reader that resolved the old link, older ones are removed."""
    target = Path(config['output_dir'])
    target.mkdir(parents=True, exist_ok=True)
    group = grp.getgrnam(config.get('cert_group', 'root')).gr_gid
    os.chmod(target, 0o750)
    if os.geteuid() == 0:
        os.chown(target, -1, group)
    generation = target / cert['fingerprint']
    current = target / 'current'
    previous = os.readlink(current) if current.is_symlink() else None
    if previous != generation.name:
        generation.mkdir(mode=0o750, exist_ok=True)
        atomic(generation / 'fullchain.pem', Path(cert['cert']).read_text(), 0o644)
        atomic(generation / 'privkey.pem', Path(cert['key']).read_text(), 0o640)
        if os.geteuid() == 0:
            os.chown(generation, -1, group)
            os.chown(generation / 'privkey.pem', -1, group)
        pending = target / 'current.new'
        pending.unlink(missing_ok=True)
        pending.symlink_to(generation.name)
        pending.replace(current)
    for name in ('fullchain.pem', 'privkey.pem'):
        link = target / name
        if not link.is_symlink():
            link.symlink_to(f'current/{name}')
    for old in target.iterdir():
        if (old.is_dir() and not old.is_symlink() and re.fullmatch(r'[0-9a-f]{64}', old.name)
                and old.name not in (generation.name, previous)):
            shutil.rmtree(old)


def safe_error(records, host):
    """Never forward raw ACME response bodies, URLs or account/challenge data."""
    found = None
    for record in records:
        if record.get('level') != 'error' or not record.get('logger', '').startswith('tls.'):
            continue
        identifiers = record.get('identifiers', [record.get('identifier', '')])
        if host not in identifiers:
            continue
        raw = str(record.get('error', ''))
        problem = re.search(r'urn:ietf:params:acme:error:([a-zA-Z]+)', raw)
        code = re.search(r'\bHTTP (\d{3})\b', raw)
        found = ('ACME: ' + problem[1] if problem else
                 'ACME: HTTP ' + code[1] if code else
                 'ACME: connection refused' if 'connection refused' in raw else
                 'ACME: timeout' if 'timeout' in raw or 'deadline exceeded' in raw else
                 'ACME: certificate operation failed')
    return found


def utc(seconds):
    return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(seconds))


def observe(config, state, cert, error, now):
    """Persist events before delivery; failure stays latched until a new cert."""
    queue = state.setdefault('pending', [])
    fingerprint = cert['fingerprint'] if cert else None
    if cert:
        state['expires'] = cert['end']
    recovered = state.get('failed') and cert and fingerprint != state.get('failed_fingerprint')
    if recovered:
        queue.append({'type': 'system_alert', 'subject': 'TLS: обновлено',
                      'detail': f"Сертификат {config['host']}; срок истечения: {utc(cert['end'])}",
                      'deliveryId': str(uuid.uuid4())})
        state['failed'] = False
        state.pop('last_alert', None)
    elif error:
        if not state.get('failed'):
            state['failed_fingerprint'] = fingerprint
        state['failed'] = True
        state['error'] = error
        if now - state.get('last_alert', -1e12) >= config.get('notify_hours', 6) * 3600:
            expiry = utc(state['expires']) if state.get('expires') else 'сертификата нет'
            queue.append({'type': 'system_alert', 'subject': 'TLS: ошибка выпуска/обновления',
                          'detail': f"{config['host']}: {error}; срок истечения: {expiry}",
                          'deliveryId': str(uuid.uuid4())})
            state['last_alert'] = now
    # Bound local backlog if the bot is disabled for a long period.
    state['pending'] = queue[-32:]


def deliver(config, state):
    """Entry: the Telegram bot's event endpoint. Exits have no bot outside an
    emergency, so events stay queued in state.json (bot_secret unset)."""
    try:
        secret = read_text(config['bot_secret']).strip() if config.get('bot_secret') else ''
        if not secret:
            return
        while state.get('pending'):
            event = state['pending'][0]
            request = urllib.request.Request(config.get('bot_url', 'http://127.0.0.1:8877/event'),
                                             data=json.dumps(event).encode(),
                                             headers={'Content-Type': 'application/json',
                                                      'X-Tgbot-Secret': secret})
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.load(response)
                if not result.get('ok') or 'event_id' not in result:
                    return
            state['pending'].pop(0)
    except (OSError, ValueError):
        pass


def journal(state):
    command = ['journalctl', '-u', 'caddy', '-o', 'json', '--no-pager', '-n', '500']
    if state.get('cursor'):
        command += ['--after-cursor', state['cursor']]
    else:
        command += ['--since', '5 minutes ago']
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return []  # No journal yet: the next tick reads it.
    records = []
    for line in result.stdout.splitlines():
        entry = json.loads(line)
        state['cursor'] = entry['__CURSOR']
        try:
            records.append(json.loads(entry.get('MESSAGE', '')))
        except ValueError:
            continue
    return records


def activate(config, cert):
    """Apply the window of the issued certificate; panel role also turns on
    the redirect and the Secure flag. Any failure restores the old files."""
    directory = Path(config['caddy_dir'])
    files = {directory / 'vpn2-tls.conf': tls_config(config, cert['end'] - cert['start'])}
    secure_file = config.get('secure_file')
    if secure_file:
        files[directory / 'vpn2-http.conf'] = 'redir https://' + authority(config['host']) + '{uri} permanent\n'
        files[Path(secure_file)] = 'true\n'
    previous = {path: read_text(path) for path in files}
    if previous == files:
        return
    restart = bool(secure_file and config.get('panel_container')
                   and previous[Path(secure_file)].strip() != 'true')
    for path, text in files.items():
        atomic(path, text, 0o644)
    try:
        subprocess.run(['caddy', 'validate', '--config', str(directory / 'Caddyfile')],
                       capture_output=True, check=True, timeout=20)
        # The panel startup wrapper rereads the flag on restart (docker restart
        # alone cannot change container environment). CSRF uses the same flag.
        if restart:
            subprocess.run(['docker', 'restart', config['panel_container']],
                           capture_output=True, check=True, timeout=60)
        subprocess.run(['systemctl', 'reload', 'caddy'], capture_output=True, check=True, timeout=30)
    except subprocess.SubprocessError:
        for path, text in previous.items():
            atomic(path, text, 0o644)
        if restart:
            subprocess.run(['docker', 'restart', config['panel_container']],
                           capture_output=True, check=True, timeout=60)
        raise


def tick(config, state_path, initial=False):
    state = read_json(state_path, {})
    now = time.time()
    cert = current_certificate(config, now)
    error = safe_error(journal(state), config['host'])
    if initial and not cert:
        error = error or f"ACME: initial issuance did not complete within {config.get('wait', 60)}s"
    if cert:
        export_certificate(config, cert)
        activate(config, cert)
    observe(config, state, cert, error, now)
    atomic(state_path, json.dumps(state))
    deliver(config, state)
    atomic(state_path, json.dumps(state))
    if initial:
        print(f"TLS: HTTPS готов, сертификат до {utc(cert['end'])}" if cert
              else f"TLS: HTTP сохранён; {state.get('error', 'ACME pending')}; Caddy повторит выпуск сам")
    return bool(cert)


def prepare(args):
    authority(args.host)
    previous = read_json(args.config, {})
    caddy_dir = Path(args.caddy_dir)
    config = {'role': args.role, 'host': args.host, 'output_dir': args.output_dir,
              'acme_url': args.acme_url, 'acme_root': args.acme_root, 'storage': args.storage,
              'caddy_dir': str(caddy_dir), 'notify_hours': 6, 'cert_group': args.cert_group}
    if args.role == 'panel':
        config.update(secure_file=args.secure_file, panel_container=args.panel_container,
                      bot_secret=args.bot_secret)
    else:
        config.update(local_port=args.local_port)
    atomic(args.config, json.dumps(config))
    atomic(caddy_dir / 'vpn2-tls.conf', tls_config(config), 0o644)
    if args.role == 'exit':
        atomic(caddy_dir / 'Caddyfile', exit_caddyfile(config), 0o644)
    elif previous.get('host') != args.host or not (caddy_dir / 'vpn2-http.conf').exists():
        # Until the first certificate for this host the panel stays on HTTP.
        atomic(caddy_dir / 'vpn2-http.conf', 'import internal_only\nencode gzip\nimport to_panel\n', 0o644)
        atomic(args.secure_file, 'false\n', 0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/vpn2/tls/config.json')
    parser.add_argument('--prepare', action='store_true', help='write config and Caddy snippets')
    parser.add_argument('--initial', action='store_true', help='wait for the first certificate')
    parser.add_argument('--wait', type=int, default=60, help='seconds for --initial')
    parser.add_argument('--bot-secret', default='/etc/anysda/tgbot-secret.txt')
    parser.add_argument('--role', choices=('panel', 'exit'), default='panel')
    parser.add_argument('--host')
    parser.add_argument('--output-dir', default='/etc/vpn2/tls/ip')
    parser.add_argument('--acme-url', default=LE)
    parser.add_argument('--acme-root', default='')
    parser.add_argument('--storage', default='/var/lib/caddy/.local/share/caddy')
    parser.add_argument('--caddy-dir', default='/etc/caddy')
    parser.add_argument('--secure-file', default='/etc/vpn2/tls/panel/secure')
    parser.add_argument('--panel-container', default='')
    parser.add_argument('--cert-group', default='root')
    parser.add_argument('--local-port', type=int, default=8443)
    args = parser.parse_args()
    if args.prepare:
        prepare(args)
        return
    config = read_json(args.config, {})
    state_path = Path(args.config).with_name('state.json')
    with state_path.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.initial:
            config['wait'] = args.wait
            deadline = time.monotonic() + args.wait
            while not current_certificate(config, time.time()) and time.monotonic() < deadline:
                time.sleep(2)
        tick(config, state_path, args.initial)


if __name__ == '__main__':
    main()
