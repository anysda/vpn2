"""vpn2 tls: Let's Encrypt lifecycle on top of Caddy, shared by the entry and exits.

Caddy obtains and renews certificates (HTTP-01 on TCP 80; HTTP/3 stays off).
This command, run once a minute by vpn2-tls.timer:
  * pins the renewal window (IP: 5 days after issuance, domain: 7 days
    before expiry) from the lifetime of the certificate actually issued;
  * exports the current pair to <output_dir>/{fullchain,privkey}.pem;
  * panel role: the HTTPS site serves the CA pair, without one a stable
    self-signed pair; a switch is a file swap and `caddy reload`;
  * queues an admin alert on an ACME failure (error, expiry of the current
    certificate; at most once per notify_hours) and "updated" after recovery.
"""

import argparse
import contextlib
import grp
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import time
import uuid

from vpn2 import notify
from vpn2.fsutil import atomic, lock, read_json

LE = 'https://acme-v02.api.letsencrypt.org/directory'
DAY = 86400
# Let's Encrypt "shortlived" profile: 160 hours.
SHORTLIVED = 160 * 3600
IP_RENEW_AFTER = 5 * DAY
DOMAIN_RENEW_BEFORE = 7 * DAY
# The pair the panel site serves; extra routes (the install wizard) go before the panel. With a domain the
# entry IP may have a site of its own (the wizard keeps its IP pair while the panel moves to the domain);
# default_sni is the entry IP then: a browser opening the IP sends no SNI.
SITE_CERT, SITE_KEY, ROUTES, SITES = 'vpn2-site.crt', 'vpn2-site.key', 'vpn2-site.d', 'vpn2-sites.d'
PANEL_CADDYFILE = '''{
    # :80 только переадресует на HTTPS. Сайт панели без имени отдаёт выгруженную пару (CA или
    # самоподписанную); ACME - на сайте loopback: два сайта с одним именем Caddy не принимает,
    # а без ignore_loaded_certs не выпускает имя, для которого пара уже загружена. Одной строкой:
    # второй auto_https заменил бы первый.
    auto_https disable_redirects ignore_loaded_certs
    default_sni @SNI@
    # Без HTTP/3: Caddy держал бы *:443/udp, нужный sing-box для возврата UDP через tproxy.
    servers {
        protocols h1 h2
    }
}
# Пути бота - только для самой ноды, бот ходит на 127.0.0.1:51821 мимо Caddy.
(internal_only) {
    @internal path /api/bot /api/bot/* /api/ops/bot-snapshot
    respond @internal 404
}
# X-Forwarded-For ставит только Caddy: по нему панель считает лимит попыток входа.
(to_panel) {
    reverse_proxy 127.0.0.1:51821 {
        header_up X-Forwarded-For {remote_host}
        transport http {
            keepalive off
        }
    }
}
https://@AUTHORITY@:@PORT@ {
    bind 127.0.0.1
    import @DIR@/vpn2-tls.conf
    respond 404
}
https://:443 {
    tls @DIR@/vpn2-site.crt @DIR@/vpn2-site.key
    import @DIR@/vpn2-site.d/*.conf
    @foreign not host @HOST@
    abort @foreign
    import internal_only
    encode gzip
    import to_panel
}
:80 {
    redir https://@AUTHORITY@{uri} permanent
}
'''


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


def panel_caddyfile(config):
    replace = {'@HOST@': config['host'], '@SNI@': config.get('sni') or config['host'], '@AUTHORITY@': authority(config['host']),
               '@PORT@': str(config.get('local_port', 8443)), '@DIR@': config['caddy_dir']}
    text = re.sub('|'.join(replace), lambda match: replace[match[0]], PANEL_CADDYFILE)
    # На IP сайт IP - это сайт панели (второй с тем же именем Caddy не принимает: у ACME уже есть).
    return text if is_ip(config['host']) else f"{text}import {config['caddy_dir']}/{SITES}/*.conf\n"


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


def self_signed(config):
    """One pair kept on disk while there is no CA certificate: a printed fingerprint stays true
    across deploys. A new one only for another host or after its ten years."""
    folder = Path(config['output_dir']).parent / 'self-signed'
    cert, key, host = folder / 'fullchain.pem', folder / 'privkey.pem', config['host']
    try:
        found = certificate(cert, key, host, time.time())
    except (OSError, ValueError, ssl.SSLError, subprocess.SubprocessError):
        found = None
    if found:
        return found
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                    '-days', '3650', '-subj', f'/CN={host}', '-addext',
                    f"subjectAltName={'IP' if is_ip(host) else 'DNS'}:{host}", '-keyout', key, '-out', cert],
                   check=True, capture_output=True, timeout=30)
    return certificate(cert, key, host, time.time())


def fingerprint(cert):
    """SHA-256 of the leaf as a browser and `openssl x509 -fingerprint -sha256` show it."""
    der = ssl.PEM_cert_to_DER_cert(Path(cert).read_text().split('-----END CERTIFICATE-----')[0]
                                   + '-----END CERTIFICATE-----\n')
    return ':'.join(f'{byte:02X}' for byte in hashlib.sha256(der).digest())


def notice(state):
    """The one line the deploy and 99-verify print while the panel serves the self-signed pair."""
    return f"сертификат самоподписанный, отпечаток SHA-256 {state['self_signed']}" if state.get('self_signed') else ''


def current_certificate(config, now):
    candidates = []
    # Caddy storage sanitizes IPv6 colons into underscores.
    name = config['host'].lower().replace(':', '_')
    for cert in Path(config['storage']).glob(f'certificates/*/{name}/{name}.crt'):
        try:
            metadata = read_json(cert.with_suffix('.json'), {})
            # Caddy's internal issuer stores "issuer_data": null.
            if (metadata.get('issuer_data') or {}).get('ca') != config.get('acme_url', LE):
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
    # Only root changes the group; a test run as a user has no such group.
    group = grp.getgrnam(config.get('cert_group', 'root')).gr_gid if os.geteuid() == 0 else None
    os.chmod(target, 0o750)
    if group is not None:
        os.chown(target, -1, group)
    generation = target / cert['fingerprint']
    current = target / 'current'
    previous = os.readlink(current) if current.is_symlink() else None
    if previous != generation.name:
        generation.mkdir(mode=0o750, exist_ok=True)
        atomic(generation / 'fullchain.pem', Path(cert['cert']).read_text(), 0o644)
        atomic(generation / 'privkey.pem', Path(cert['key']).read_text(), 0o640)
        if group is not None:
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
        retry = re.search(r'retry after (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC)', raw)
        found = ('ACME: ' + problem[1] + (f' (HTTP {code[1]})' if code else '') if problem else
                 'ACME: HTTP ' + code[1] if code else
                 'ACME: connection refused' if 'connection refused' in raw else
                 'ACME: timeout' if 'timeout' in raw or 'deadline exceeded' in raw else
                 'ACME: certificate operation failed') + (f', повтор после {retry[1]}' if retry else '')
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
        state['error'], state['failed_host'] = error, config['host']
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
    if not config.get('bot_secret'):
        return
    while state.get('pending'):
        sent = notify.bot_event(state['pending'][0], secret_file=config['bot_secret'],
                                url=config.get('bot_url', notify.BOT_URL))
        if sent is not notify.Outcome.SENT:
            return
        state['pending'].pop(0)


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


def serve(config, cert, reload=True):
    """Apply the window of the issued certificate; the panel site gets the CA pair, without one
    the self-signed pair. Only a difference is written and reloaded: a reload restarts Caddy's
    ACME job, its back-off would be lost. Any failure restores the old files. Returns the pair
    the panel site serves (None on an exit)."""
    directory = Path(config['caddy_dir'])
    files = {directory / 'vpn2-tls.conf': tls_config(config, cert and cert['end'] - cert['start'])}
    pair = None
    if config.get('role') == 'panel':
        pair = cert or self_signed(config)
        files[directory / SITE_CERT] = Path(pair['cert']).read_text()
        files[directory / SITE_KEY] = Path(pair['key']).read_text()

    def write(texts):
        for path, text in texts.items():
            atomic(path, text, 0o640 if path.name == SITE_KEY else 0o644)
        # Caddy reads the key as its own user; a test run as a user has no such group.
        if SITE_KEY in (path.name for path in texts) and os.geteuid() == 0:
            os.chown(directory / SITE_KEY, -1, grp.getgrnam('caddy').gr_gid)

    previous = {path: read_text(path) for path in files}
    if previous != files:
        write(files)
        try:
            if reload:
                subprocess.run(['caddy', 'validate', '--config', str(directory / 'Caddyfile')],
                               capture_output=True, check=True, timeout=20)
                subprocess.run(['systemctl', 'reload', 'caddy'], capture_output=True, check=True, timeout=30)
        except subprocess.SubprocessError:
            write(previous)
            raise
    return pair


def tick(config, state_path, initial=False):
    state = read_json(state_path, {})
    now = time.time()
    cert = current_certificate(config, now)
    error = safe_error(journal(state), config['host'])
    if initial and not cert:
        error = (error or (state.get('error') if state.get('failed') else None)
                 or f"ACME: initial issuance did not complete within {config.get('wait', 60)}s")
    if cert:
        export_certificate(config, cert)
    if cert or config.get('role') == 'panel':
        pair = serve(config, cert)
        state.pop('self_signed', None)
        if pair and not cert:
            state['self_signed'] = fingerprint(pair['cert'])
            # The unit journal names the fallback once per pair; --initial prints to the installation only.
            if not initial and state.get('journaled') != state['self_signed']:
                print(f'TLS: {notice(state)}; CA-сертификат встанет сам после выпуска')
                state['journaled'] = state['self_signed']
        else:
            state.pop('journaled', None)
    observe(config, state, cert, error, now)
    atomic(state_path, json.dumps(state))
    deliver(config, state)
    atomic(state_path, json.dumps(state))
    if initial and config.get('role') == 'exit':
        print(f"TLS: сертификат на IP до {utc(cert['end'])}" if cert
              else f"TLS: сертификата на IP пока нет; {state.get('error', 'ACME pending')}; "
                   'Caddy повторит выпуск сам, до него авария на самоподписанном')
    elif initial and cert:
        print(f"TLS: HTTPS готов, сертификат до {utc(cert['end'])}")
    elif initial:
        print(f"TLS: {notice(state)}; {state.get('error', 'ACME pending')}; "
              'Caddy повторит выпуск сам, CA-сертификат встанет без перезапуска панели')
    return bool(cert)


def prepare(args):
    authority(args.host)
    caddy_dir = Path(args.caddy_dir)
    folders = [Path(args.config).parent, caddy_dir] + ([Path(args.secure_file).parent] if args.role == 'panel' else [])
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
    config = {'role': args.role, 'host': args.host, 'output_dir': args.output_dir,
              'acme_url': args.acme_url, 'acme_root': args.acme_root, 'storage': args.storage,
              'caddy_dir': str(caddy_dir), 'notify_hours': 6, 'cert_group': args.cert_group,
              'local_port': args.local_port}
    if args.sni and args.sni != args.host:
        config['sni'] = args.sni
    if args.role == 'panel':
        config.update(secure_file=args.secure_file, bot_secret=args.bot_secret)
    atomic(args.config, json.dumps(config))
    if args.role == 'exit':
        atomic(caddy_dir / 'vpn2-tls.conf', tls_config(config), 0o644)
        atomic(caddy_dir / 'Caddyfile', exit_caddyfile(config), 0o644)
    else:
        # The panel is only ever behind HTTPS: Secure cookie and https CSRF origin from the start.
        atomic(args.secure_file, 'true\n', 0o644)
        # The fallback pair exists on every install path, a later expiry serves it at once.
        self_signed(config)
        serve(config, current_certificate(config, time.time()), reload=False)
        atomic(caddy_dir / 'Caddyfile', panel_caddyfile(config), 0o644)
        # Gate and redirect snippets of 1.2.0: the stage guards them, a failed run gets them back.
        for name in ('vpn2-panel.conf', 'vpn2-http.conf'):
            (caddy_dir / name).unlink(missing_ok=True)


def install_caddy():
    """Caddy of the pinned version now, before any stage (stage 30 re-asserts the same)."""
    from vpn2 import deploy
    if deploy.svc('caddy_install'):
        raise RuntimeError('caddy: установка не прошла')


def ip_certificate(host, wait=60, acme_url=LE, acme_root='', config='/etc/vpn2/tls/config.json',
                   state='/etc/vpn2/tls/state.json'):
    """The panel HTTPS site on the entry IP before any stage: a CA certificate within `wait`
    seconds, else the self-signed pair. Returns the self-signed fingerprint, '' for a CA one.
    Stage 30 already moved the panel to a domain: its site stays as is. Silent: the caller prints the outcome."""
    if is_ip(read_json(config, {}).get('host') or host):
        with contextlib.redirect_stdout(io.StringIO()):
            main(['--prepare', '--host', host, '--acme-url', acme_url, '--acme-root', acme_root])
            subprocess.run(['systemctl', 'reload-or-restart', 'caddy'], check=True, capture_output=True, timeout=60)
            main(['--initial', '--wait', str(wait)])
    return read_json(state, {}).get('self_signed', '')


def serve_routes(name, routes, caddy_dir='/etc/caddy', site=''):
    """Routes ahead of the panel on its HTTPS site, kept by stage 30's Caddyfile; '' removes them.
    Write `route { ... }` / `handle { ... }` blocks: Caddy orders them before the panel proxy.
    site - a site block of its own (`https://<entry IP> { ... }`), live only while the panel is on a domain;
    the Caddyfile snippets (to_panel) work in both."""
    if not re.fullmatch(r'[a-z0-9-]+', name):
        raise ValueError('invalid routes name')
    paths = {Path(caddy_dir) / ROUTES / f'{name}.conf': routes, Path(caddy_dir) / SITES / f'{name}.conf': site}
    previous = {path: read_text(path) for path in paths}
    if previous == paths:
        return

    def write(texts):
        for path, text in texts.items():
            if text:
                path.parent.mkdir(exist_ok=True)
                atomic(path, text, 0o644)
                continue
            path.unlink(missing_ok=True)
            if path.parent.name == SITES and path.parent.is_dir() and not any(path.parent.iterdir()):
                path.parent.rmdir()

    write(paths)
    try:
        subprocess.run(['caddy', 'validate', '--config', str(Path(caddy_dir) / 'Caddyfile')],
                       capture_output=True, check=True, timeout=20)
        subprocess.run(['systemctl', 'reload', 'caddy'], capture_output=True, check=True, timeout=30)
    except subprocess.SubprocessError:
        write(previous)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(prog='vpn2 tls', description=__doc__)
    parser.add_argument('--config', default='/etc/vpn2/tls/config.json')
    parser.add_argument('--prepare', action='store_true', help='write config and Caddy snippets')
    parser.add_argument('--initial', action='store_true', help='wait for the first certificate')
    parser.add_argument('--wait', type=int, default=60, help='seconds for --initial')
    parser.add_argument('--bot-secret', default='/run/vpn2-secrets/root/tgbot_secret')
    parser.add_argument('--role', choices=('panel', 'exit'), default='panel')
    parser.add_argument('--host')
    parser.add_argument('--sni', default='', help='entry IP: the name of a handshake without SNI')
    parser.add_argument('--output-dir', default='/etc/vpn2/tls/ip')
    parser.add_argument('--acme-url', default=LE)
    parser.add_argument('--acme-root', default='')
    parser.add_argument('--storage', default='/var/lib/caddy/.local/share/caddy')
    parser.add_argument('--caddy-dir', default='/etc/caddy')
    parser.add_argument('--secure-file', default='/etc/vpn2/tls/panel/secure')
    parser.add_argument('--cert-group', default='root')
    parser.add_argument('--local-port', type=int, default=8443)
    args = parser.parse_args(argv)
    if args.prepare:
        prepare(args)
        return
    config = read_json(args.config, {})
    state_path = Path(args.config).with_name('state.json')
    with lock(state_path.with_suffix('.lock')):
        if args.initial:
            config['wait'] = args.wait
            # Caddy повторяет отказанный выпуск не раньше чем через минуту: ждать есть смысл,
            # только пока выпуск на этот хост не отказал - ни в прошлый раз, ни сейчас.
            state = read_json(state_path, {})
            seen = dict(state)
            failed = state.get('failed') and state.get('failed_host') == config['host']
            deadline = time.monotonic() + (0 if failed else args.wait)
            while (not current_certificate(config, time.time()) and time.monotonic() < deadline
                   and not safe_error(journal(seen), config['host'])):
                time.sleep(2)
        tick(config, state_path, args.initial)
