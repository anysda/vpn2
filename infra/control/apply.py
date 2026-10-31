#!/usr/bin/env python3
"""Apply a managed config through a candidate.

Candidate beside the target -> syntax and project checks -> journal -> rename ->
reload -> verify -> only then "last good". A failed apply restores the last
good structure with the CURRENT access state: WireGuard peers come from the
live file, OpenVPN CRL/ccd and IKEv2 secrets are separate files never kept
here. An interrupted apply is finished by the next run (or `resume`).

Exit codes: 2 candidate rejected (target untouched), 3 not applied and the
previous config restored, 4 restore failed, 5 first install failed.
"""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

RESOURCES = {
    'sing-box': {'path': '/etc/sing-box/config.json', 'mode': 0o600,
                 'check': ['/usr/local/bin/sing-box', 'check', '-c'],
                 'apply': [['systemctl', 'restart', 'sing-box']], 'unit': 'sing-box',
                 'verify': [['systemctl', 'is-active', '--quiet', 'sing-box']]},
    'openvpn': {'path': '/etc/openvpn/server/server.conf', 'mode': 0o644,
                'apply': [['systemctl', 'restart', 'openvpn-server@server']],
                'unit': 'openvpn-server@server',
                'verify': [['systemctl', 'is-active', '--quiet', 'openvpn-server@server']]},
    'ikev2': {'path': '/etc/swanctl/conf.d/anysda.conf', 'mode': 0o644,
              'apply': [['/usr/sbin/swanctl', '--load-all', '--clear', '--noprompt']],
              'verify': [['/usr/sbin/swanctl', '--list-conns']]},
    'wg': {'path': '/etc/wireguard/wg0.conf', 'mode': 0o600,
           'verify': [['/usr/bin/wg', 'show', 'wg0', 'listen-port']]},
}
KEY = r'[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]='
# C isspace() within a line: what wg(8) skips and openvpn splits words at. Python's
# \s and str.strip() also take \x1c, \x85, NBSP..., which both read as text.
BLANK = r'[ \t\v\f\r]'
# The keys wg syncconf gets, with the values wg reads for them once BLANK is gone.
# wg reads key and section names in any ASCII case (strncasecmp). A minus before an address removes it.
WG_ADDRESS = '-?[0-9A-Fa-f.:]+(?:/[0-9]+)?'
WG_VALUES = {'PrivateKey': KEY, 'ListenPort': '[0-9]+', 'FwMark': '(?:(?ai:off)|0x[0-9A-Fa-f]+|[0-9]+)',
             'PublicKey': KEY, 'PresharedKey': KEY, 'AllowedIPs': f'{WG_ADDRESS}(?:,{WG_ADDRESS})*'}
WG_SECTIONS = {'[Interface]': ('PrivateKey', 'ListenPort', 'FwMark'),
               '[Peer]': ('PublicKey', 'PresharedKey', 'AllowedIPs')}
# The directives infra/scripts/29-openvpn.sh writes, in every version so far:
# restore() checks a last good config an older version wrote. openvpn reads a
# directive past the first word too (setenv opt, a BOM, config), and some run
# while it parses (log, plugin, providers): only a known name is let through.
OPENVPN_DIRECTIVES = frozenset((
    'port', 'proto', 'dev', 'topology', 'server', 'ifconfig-pool', 'route',
    'ca', 'cert', 'key', 'dh', 'tls-crypt', 'crl-verify', 'client-config-dir',
    'keepalive', 'cipher', 'data-ciphers', 'auth', 'persist-key', 'persist-tun',
    'sndbuf', 'rcvbuf', 'fast-io', 'user', 'group', 'push', 'status',
    'status-version', 'management', 'management-client-user', 'verb'))


class Failed(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}


def run(argv, data=None):
    result = subprocess.run(argv, input=data, capture_output=True, text=True, timeout=60, env=ENV)
    if result.returncode:
        last = (result.stderr or result.stdout).strip().splitlines()
        # nft ends its message with the source line and a caret, openvpn with a
        # hint to --help; the cause is above.
        last = [line for line in last if 'error' in line.lower()][:1] or last
        raise RuntimeError(f'{argv[0]} {argv[1] if len(argv) > 1 else ""}: '
                           f'{last[-1] if last else "rc=" + str(result.returncode)}')
    return result.stdout


KNOWN_CIPHER = 'AES-256-GCM'  # every openvpn 2.6/2.7 server starts with it
CHECK_SECONDS = 10  # the check server exits in milliseconds


class CheckNotStarted(RuntimeError):
    """openvpn gives no answer on the cipher here: the cipher is not judged."""


def tls_server_starts(scratch, cipher):
    """openvpn takes this cipher line as a TLS server reads it.

    A server keeps --cipher for clients without cipher negotiation, BF-CBC included,
    while --test-crypto takes it as a static key cipher, which OpenSSL 3 refuses for
    BF-CBC without the legacy provider. openvpn 2.6 and 2.7 refuse an unknown cipher
    before they open the tun, so on the tun node /nonexistent the server exits by
    itself: no root, no tun, no process left. The cipher is taken when the start
    ends on the line it ends on with KNOWN_CIPHER.
    """
    cert, key = scratch / 'tls.crt', scratch / 'tls.key'
    if not cert.exists():
        run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
             '-nodes', '-keyout', str(key), '-out', str(cert), '-subj', '/CN=check', '-days', '1'])
    known = tls_server_end(cert, key, KNOWN_CIPHER)
    if '/nonexistent' not in known:
        raise CheckNotStarted(f'openvpn --cipher {KNOWN_CIPHER}: {known}')
    return cipher == KNOWN_CIPHER or tls_server_end(cert, key, cipher) == known


# openvpn stamps each line it prints outside a daemon: 2.6/2.7, then older versions.
STAMP = re.compile(r'(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d|\w{3} \w{3} [ \d]\d \d\d:\d\d:\d\d \d{4}) ')


def tls_server_end(cert, key, cipher):
    """The line a server start with this cipher gives up on, without its time stamp."""
    try:
        result = subprocess.run(['/usr/sbin/openvpn', '--verb', '1', '--dev', 'tun', '--dev-node', '/nonexistent',
                                 '--disable-dco', '--port', '0', '--server', '10.67.67.0', '255.255.255.0',
                                 '--ca', str(cert), '--cert', str(cert), '--key', str(key), '--dh', 'none',
                                 '--cipher', cipher],
                                capture_output=True, text=True, timeout=CHECK_SECONDS, env=ENV)
    except subprocess.TimeoutExpired:  # run() has killed the server
        raise CheckNotStarted(f'openvpn --cipher {cipher}: no answer in {CHECK_SECONDS} s') from None
    lines = [STAMP.sub('', line, count=1).strip() for line in (result.stdout + result.stderr).splitlines()]
    lines = [line for line in lines if line and not line.startswith(('Exiting due to fatal error', 'Use --help'))]
    return lines[-1] if lines else f'rc={result.returncode}'


class Stopped(SystemExit):
    """SIGTERM, SIGHUP or SIGINT in an install or a restore: 128+sig, after the cleanup."""


SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def stop_on_signal(number, _frame):
    # The default handlers skip finally and leave the check server and its keys; the
    # signals after the first are ignored until exit, so nothing cuts the cleanup short.
    for each in SIGNALS:
        signal.signal(each, signal.SIG_IGN)
    raise Stopped(128 + number)


@contextlib.contextmanager
def stopping():
    """A signal inside stops apply through its finally and except clauses.

    A signal the caller ignores (nohup, a command run with "&") stays ignored.
    """
    main = threading.current_thread() is threading.main_thread()
    previous = {number: signal.signal(number, stop_on_signal) for number in SIGNALS
                if signal.getsignal(number) is not signal.SIG_IGN} if main else {}
    try:
        yield
    except Stopped:
        previous = {}  # apply exits: the signals stay ignored until then
        raise
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def discard(path):
    """Delete a temporary file or directory whole: a signal waits until it is gone."""
    before = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            Path(path).unlink(missing_ok=True)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)


def write(path, body, mode):
    descriptor, name = tempfile.mkstemp(prefix='.vpn2-apply-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            os.fchmod(output.fileno(), mode)
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        discard(name)


SPACE = ' \t\n\v\f\r\0'  # C isspace() and the terminator, not Python's wider isspace


def openvpn_words(line):
    """A config line split into words as openvpn 2.7 parse_line() does it.

    A quote, # and ; mean something only at the start of a word; a backslash
    escapes outside single quotes and may only precede \\, " or a space.
    """
    words, word, state, escaped = [], '', None, False  # state: None, '' bare, '"' or "'"
    for char in line + '\n\0':  # openvpn reads the newline and the terminator too
        if char == '\\' and not escaped and state != "'":
            escaped = True
            continue
        out, done = '', False
        if state is None:
            if char in '#;':
                break
            if char not in SPACE:
                if char in '"\'' and not escaped:
                    state = char
                else:
                    out, state = char, ''
        elif state == '':
            done = char in SPACE and not escaped
            out = '' if done else char
        else:
            done = char == state and not (escaped and state == '"')
            out = '' if done or char == '\0' else char
        if escaped and out and out not in '\\"' + SPACE:
            raise ValueError(f'bad backslash in: {line}')
        escaped = False
        word += out
        if done:
            words.append(word)
            word, state = '', None
    if state:
        raise ValueError(f'no closing quotation ({state}) in: {line}')
    return words


def wg_line(line):
    """The section or syncconf key wg(8) reads in a line (None if neither) and its value.

    wg cuts the line at the first #, drops BLANK anywhere in it and compares the
    name in ASCII case only (wireguard-tools config.c: config_read_line, process_line).
    """
    seen = re.sub(BLANK, '', line.split('#', 1)[0])
    for name in WG_SECTIONS:
        if re.fullmatch(re.escape(name), seen, re.I | re.A):
            return name, ''
    for name in WG_VALUES:
        if re.match(name + '=', seen, re.I | re.A):
            return name, seen[len(name) + 1:]
    return None, seen


def wg_split(text):
    """Deployment-owned [Interface] section and the access-owned [Peer] tail."""
    lines = text.split('\n')
    cut = next((index for index, line in enumerate(lines) if wg_line(line)[0] == '[Peer]'), None)
    if cut is None:
        return text.rstrip(' \t\n\v\f\r') + '\n', ''
    return '\n'.join(lines[:cut]).rstrip(' \t\n\v\f\r') + '\n', '\n'.join(lines[cut:])


class Applier:
    def __init__(self, root=Path('/'), state=Path('/var/lib/vpn2-apply'), command=run, settle=5):
        self.root = root
        self.state = state
        self.command = command
        # is-active right after restart is "active" even for a service that
        # dies on bind a moment later and loops in Restart=.
        self.settle = settle

    def target(self, resource):
        return self.root / RESOURCES[resource]['path'].lstrip('/')

    def journal(self, resource):
        return self.state / (resource + '.journal')

    def good(self, resource):
        return self.state / (resource + '.good')

    def candidate(self, resource):
        target = self.target(resource)
        return target.parent / ('.vpn2-apply-' + target.name + '.new')

    def stage(self, resource, value):
        write(self.journal(resource), json.dumps({'stage': value}), 0o600)

    # Project checks: what a syntax check cannot see.
    def check(self, resource, text, path):
        spec = RESOURCES[resource]
        if resource == 'sing-box':
            # The same port on 127.0.0.1 and ::1 is two sockets (agh-doh-v4/v6).
            sockets = [(item.get('listen'), item['listen_port'], item.get('network'))
                       for item in json.loads(text).get('inbounds', []) if 'listen_port' in item]
            if len(sockets) != len(set(sockets)):
                raise ValueError('two inbounds on one port')
        elif resource == 'openvpn':
            options, ciphers, managed_lines = {}, {}, []
            # openvpn drops a UTF-8 BOM at the start of the file, nowhere else, and
            # ends a line only at a newline: splitlines() also breaks at \x1c, \x85...
            for line in text.removeprefix('\ufeff').split('\n'):
                words = openvpn_words(line)
                if words:
                    name = words[0].removeprefix('--')
                    if name not in OPENVPN_DIRECTIVES:
                        raise ValueError(f'{name} is not allowed in a candidate')
                    options.setdefault(name, words[1:])
                    # openvpn applies the last crl-verify or client-config-dir.
                    if name in ('crl-verify', 'client-config-dir'):
                        managed_lines.append(name)
                    # openvpn applies the last cipher, data-ciphers and auth, so only it is checked.
                    if name in ('cipher', 'auth'):
                        ciphers[name] = [(name, [word]) for word in words[1:]]
                    if name == 'data-ciphers':
                        ciphers[name] = [(name, word.split(':')) for word in words[1:]]
            base = self.target('openvpn').parent
            # Access is enforced only while the server reads the managed CRL and ccd.
            for name, managed in (('crl-verify', 'pki/crl.pem'), ('client-config-dir', 'ccd')):
                if managed_lines.count(name) > 1:
                    raise ValueError(f'{name} is given more than once')
                value = (options.get(name) or [''])[0]
                if (base / value).resolve() != (base / managed).resolve():
                    raise ValueError(f'{name} must be {base / managed}')
            for name in ('ca', 'cert', 'key', 'dh', 'tls-crypt', 'tls-auth', 'crl-verify'):
                value = (options.get(name) or ['none'])[0]
                if value != 'none' and not (base / value).exists():
                    raise ValueError(f'{name} {value}: no such file')
            port = (options.get('port') or ['1194'])[0]
            if not port.isdigit() or not 0 < int(port) < 65536:
                raise ValueError(f'port {port}')
            # Without this an unknown directive is found only by the restart. openvpn
            # reads the whole config before it prints the cipher list and exits, so
            # an unknown or malformed directive fails here. --test-crypto would also
            # check option combinations, but before 2.7 it demands a static key that
            # a TLS server cannot have.
            self.command(['/usr/sbin/openvpn', '--cd', str(base), '--verb', '1',
                          '--config', str(path), '--show-ciphers'])
            # The parse takes any cipher or digest name; an unknown one fails only the
            # restart. --show-ciphers prints no alias (AES256, sha256), so each name
            # goes to openvpn itself, which looks it up in OpenSSL the way the server does.
            if ciphers:
                self.check_openvpn_names([item for items in ciphers.values() for item in items])
        elif resource == 'ikev2':
            if text.count('{') != text.count('}') or not re.search(r'(?m)^\s*anysda-ikev2\s*\{', text):
                raise ValueError('connection anysda-ikev2 missing or braces unbalanced')
        elif resource == 'wg':
            # Every section and syncconf key is checked as wg reads it, the same
            # way activate() picks the lines it gives wg syncconf.
            section, public, ports = None, [], []
            for line in text.split('\n'):
                name, value = wg_line(line)
                if name in WG_SECTIONS:
                    # wg reads a later [Interface] too, but wg_split() keeps only the
                    # first one as the deployment's: an apply replaces it whole, and
                    # keys left in the peer tail would override the new header.
                    # The peer tail is always the live file's (with_access), the header
                    # always the candidate's: a key survives only in the candidate.
                    if section == '[Peer]' and name == '[Interface]':
                        raise ValueError(f'{RESOURCES["wg"]["path"]}: {line!a} after [Peer] number {len(public)}; '
                                         'move the keys under it into the [Interface] of the config you install '
                                         'and delete that section from the file')
                    section = name
                    public += [0] if name == '[Peer]' else []
                    continue
                if not name:
                    # A line Python's \s reads as a syncconf key must read the same to wg.
                    if wg_line(re.sub(r'\s', '', line))[0]:
                        raise ValueError(f'wg cannot read {line!a}')
                    continue
                if name not in WG_SECTIONS.get(section, ()):
                    raise ValueError(f'{name} in {section or "no section"}: wg cannot read {line!a}')
                if not re.fullmatch(WG_VALUES[name], value):
                    raise ValueError(f'{name}: wg cannot read {line!a}')
                # wg parse_fwmark(): strtoul() base 16 after a lowercase 0x, else 10.
                if name == 'FwMark' and value.lower() != 'off' and \
                        int(value, 16 if value.startswith('0x') else 10) > 0xffffffff:
                    raise ValueError(f'FwMark: wg takes 0/off or 0-0xffffffff, not {line!a}')
                if name == 'PublicKey':
                    public[-1] += 1
                ports += [int(value)] if name == 'ListenPort' else []
            if not any(wg_line(line)[0] == 'PrivateKey' for line in text.split('\n')):
                raise ValueError('[Interface] PrivateKey missing')
            if not ports or not all(0 < port < 65536 for port in ports):
                raise ValueError('[Interface] ListenPort missing')
            if any(count != 1 for count in public):
                raise ValueError('[Peer] without one valid PublicKey')
        if spec.get('check'):
            self.command(spec['check'] + [str(path)])

    def check_openvpn_names(self, ciphers):
        with stopping():
            scratch = None
            try:
                scratch = tempfile.mkdtemp()
                self.check_openvpn_names_with(str(Path(scratch) / 'static.key'), ciphers)
            finally:
                if scratch:
                    discard(scratch)

    def check_openvpn_names_with(self, key, ciphers):
        self.command(['/usr/sbin/openvpn', '--genkey', 'secret', key])
        for option, names in ciphers:
            # Only data-ciphers takes "?NAME": openvpn skips it when unknown, but
            # fails the start when no known cipher is left in the list. In cipher
            # and auth the "?" is part of the name, and no such name is known.
            optional = option == 'data-ciphers'
            known = [self.openvpn_accepts(key, option, name.removeprefix('?') if optional else name)
                     for name in names]
            kind = 'digest' if option == 'auth' else 'cipher'
            for name, accepted in zip(names, known):
                if name and not accepted and not (optional and name.startswith('?')):
                    raise ValueError(f'{option}: unknown {kind} {name}')
            if not any(known):
                raise ValueError(f'{option} {":".join(names)}: no known cipher')
            if option == 'data-ciphers':
                # Each name may pass and the list still fail the start: openvpn
                # 2.6 and 2.7 refuse one longer than 124 characters after it
                # drops the unknown ?NAME, so the list goes to openvpn as a whole.
                first = names[known.index(True)].removeprefix('?')
                try:
                    self.command(['/usr/sbin/openvpn', '--verb', '1', '--test-crypto', '--secret', key,
                                  '--cipher', first, '--data-ciphers', ':'.join(names)])
                except RuntimeError as error:
                    said = str(error).split(': ', 1)[-1].removeprefix('Options error: ')
                    raise ValueError(f'data-ciphers: {said or "list rejected"}') from None

    def openvpn_accepts(self, key, option, name):
        # A static key lets --test-crypto run without TLS, in 2.6 as in 2.7. An AEAD
        # cipher ignores --auth, so a digest is tried with a CBC cipher. The cipher
        # line is what a TLS server keeps for old clients, so a TLS server decides it.
        if not name:
            return False
        if option == 'cipher':
            return tls_server_starts(Path(key).parent, name)
        argv = {'data-ciphers': ['--cipher', name, '--data-ciphers', name],
                'auth': ['--cipher', 'AES-256-CBC', '--auth', name]}[option]
        try:
            self.command(['/usr/sbin/openvpn', '--verb', '1', '--test-crypto', '--secret', key] + argv)
        except RuntimeError:
            if option != 'auth':
                return False
        else:
            return True
        # --test-crypto keys an HMAC with the digest, which SHAKE and KMAC refuse,
        # while the server only looks the digest up, aliases and any case included,
        # and starts. openvpn lists each digest it finds by one name, OpenSSL gives
        # the aliases of each.
        name = name.lower()
        shown = {found.lower() for found in re.findall(r'(?m)^(\S+) \d+ bit digest size$', self.command(
            ['/usr/sbin/openvpn', '--show-digests']))}
        if name in shown:
            return True
        listed = self.command(['/usr/bin/openssl', 'list', '-digest-algorithms'])
        return any(name in group and group & shown for group in (
            {alias.strip().lower() for alias in found.split(',')}
            for found in re.findall(r'(?m)^\s*\{?\s*([^{}@=]+?)\s*\}?\s*@', listed)))

    def with_access(self, resource, structure):
        """Structure from the candidate or last good, access as it is NOW."""
        if resource != 'wg':
            return structure
        target = self.target(resource)
        current = wg_split(target.read_text())[1] if target.exists() else ''
        return wg_split(structure)[0] + ('\n' + current if current else '')

    def activate(self, resource):
        spec = RESOURCES[resource]
        if resource == 'wg':
            header, peers = wg_split(self.target(resource).read_text())
            # wg syncconf takes only wg(8) keys; Address and the like are wg-quick's.
            # wg and wg-quick end a line only at a newline, as split('\n') does.
            # Lines go as written; wg reads their comments too.
            body = '\n'.join(line for line in header.split('\n')
                              if wg_line(line)[0] in ('[Interface]', *WG_SECTIONS['[Interface]'])) + '\n' + \
                '\n'.join(line for line in peers.split('\n') if wg_line(line)[0] in ('[Peer]', *WG_SECTIONS['[Peer]']))
            self.command(['/usr/bin/wg', 'syncconf', 'wg0', '/dev/stdin'], data=body + '\n')
        for argv in spec.get('apply', []):
            self.command(argv)
        for argv in spec['verify']:
            self.command(argv)
        if 'unit' in spec:
            show = ['systemctl', 'show', '--property', 'MainPID,NRestarts', spec['unit']]
            before = self.command(show)
            time.sleep(self.settle)
            self.command(['systemctl', 'is-active', '--quiet', spec['unit']])
            if self.command(show) != before or 'MainPID=0' in before:
                raise RuntimeError(f'{spec["unit"]} did not stay running')

    def mark(self, resource):
        write(self.good(resource), self.target(resource).read_text(), 0o600)

    def install(self, resource, source):
        with stopping():
            self.install_stopping(resource, source)

    def install_stopping(self, resource, source):
        self.resume(resource)
        spec = RESOURCES[resource]
        target = self.target(resource)
        candidate = self.candidate(resource)
        try:
            try:
                body = self.with_access(resource, Path(source).read_text())
                write(candidate, body, spec['mode'])
                self.check(resource, body, candidate)
            except (OSError, ValueError, RuntimeError) as error:
                raise Failed(2, f'{resource}: candidate rejected, working config untouched: {error}')
            if not self.good(resource).exists() and target.exists():
                # Nothing verified yet: the running config earns "last good" first.
                try:
                    for argv in spec['verify']:
                        self.command(argv)
                    self.mark(resource)
                except RuntimeError:
                    pass
            self.stage(resource, 'replacing')
        except BaseException:
            # A rejection, an error or a signal, also one inside the clause above. Only a
            # journal that says "replacing" makes the next run finish the candidate.
            if self.stage_now(resource) != 'replacing':
                discard(candidate)
            raise
        self.finish(resource)

    def finish(self, resource):
        candidate = self.candidate(resource)
        if candidate.exists():
            os.replace(candidate, self.target(resource))
        self.stage(resource, 'applied')
        try:
            self.activate(resource)
        except (OSError, RuntimeError) as error:
            self.restore(resource, f'not applied: {error}')
        self.mark(resource)
        self.journal(resource).unlink()

    def restore(self, resource, reason):
        if not self.good(resource).exists():
            self.stage(resource, 'install-failed')
            raise Failed(5, f'{resource}: first install failed: {reason}')
        self.stage(resource, 'restoring')
        try:
            body = self.with_access(resource, self.good(resource).read_text())
            try:
                self.check(resource, body, self.good(resource))
            except CheckNotStarted as error:
                # Last good passed the check once; here it cannot run, which does not keep it out.
                reason += f' (last good cipher not checked: {error})'
            write(self.target(resource), body, RESOURCES[resource]['mode'])
            self.activate(resource)
        except (OSError, ValueError, RuntimeError) as error:
            self.stage(resource, 'restore-failed')
            raise Failed(4, f'{resource}: {reason}; restore failed: {error}')
        self.mark(resource)
        self.journal(resource).unlink()
        raise Failed(3, f'{resource}: {reason}; previous config restored')

    def rollback(self, resource):
        with stopping():
            self.resume(resource)
            self.restore(resource, 'rollback requested')

    def stage_now(self, resource):
        journal = self.journal(resource)
        return json.loads(journal.read_text())['stage'] if journal.exists() else None

    def resume(self, resource):
        stage = self.stage_now(resource)
        if stage is None:
            return
        if stage in ('replacing', 'applied'):
            self.finish(resource)
        elif stage in ('restoring', 'restore-failed'):
            self.restore(resource, 'interrupted restore')
        # install-failed stays visible until the next install succeeds.

    # Network rules that can cut management: a local timer, not SSH, restores
    # the project's own table; tables of ufw, Docker, fail2ban are not touched.
    def nft(self, table, source, seconds):
        text = Path(source).read_text()
        named = re.findall(r'(?m)^\s*(?:add\s+|create\s+|delete\s+|flush\s+)?table\s+(\S+)\s+(\S+)', text)
        if re.search(r'(?m)^\s*(?:flush|list)\s+ruleset\b', text) or not named or \
                any(found != ('inet', table) for found in named):
            raise Failed(2, f'nft: candidate may only change table inet {table}')
        try:
            self.command(['/usr/sbin/nft', '-c', '-f', str(source)])
        except RuntimeError as error:
            raise Failed(2, f'nft: candidate rejected, rules untouched: {error}') from None
        try:
            snapshot = self.command(['/usr/sbin/nft', 'list', 'table', 'inet', table])
        except RuntimeError:
            snapshot = ''
        restore = self.state / f'nft-{table}.restore'
        write(restore, f'table inet {table}\ndelete table inet {table}\n{snapshot}', 0o600)
        unit = f'vpn2-apply-nft-{table}'
        self.confirm(table)
        self.command(['systemd-run', '--unit', unit, f'--on-active={seconds}',
                      '/usr/sbin/nft', '-f', str(restore)])
        try:
            self.command(['/usr/sbin/nft', '-f', str(source)])
        except RuntimeError as error:
            self.command(['/usr/sbin/nft', '-f', str(restore)])
            self.confirm(table)
            raise Failed(3, f'nft: not applied: {error}; previous table restored')

    def confirm(self, table):
        unit = f'vpn2-apply-nft-{table}'
        for argv in (['systemctl', 'stop', unit + '.timer'], ['systemctl', 'reset-failed', unit]):
            try:
                self.command(argv)
            except RuntimeError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('install', 'rollback', 'mark', 'resume'):
        item = sub.add_parser(name)
        item.add_argument('resource', choices=sorted(RESOURCES))
        if name == 'install':
            item.add_argument('file')
    item = sub.add_parser('nft')
    item.add_argument('table')
    item.add_argument('file')
    item.add_argument('--seconds', type=int, default=120)
    sub.add_parser('confirm').add_argument('table')
    args = parser.parse_args()
    applier = Applier()
    applier.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (applier.state / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if args.action == 'install':
                applier.install(args.resource, args.file)
            elif args.action == 'rollback':
                applier.rollback(args.resource)
            elif args.action == 'mark':
                applier.resume(args.resource)
                for argv in RESOURCES[args.resource]['verify']:
                    applier.command(argv)
                applier.mark(args.resource)
            elif args.action == 'resume':
                applier.resume(args.resource)
            elif args.action == 'nft':
                applier.nft(args.table, args.file, args.seconds)
                print(f'nft: table inet {args.table} applied; restores in {args.seconds}s '
                      f'unless confirmed: vpn2-apply confirm {args.table}')
                return
            else:
                applier.confirm(args.table)
        except Failed as error:
            print(f'vpn2-apply: {error}', file=sys.stderr)
            sys.exit(error.code)
        except (OSError, RuntimeError) as error:
            print(f'vpn2-apply: {args.action}: {error}', file=sys.stderr)
            sys.exit(1)
    print(f'vpn2-apply: {args.action} done')


if __name__ == '__main__':
    os.umask(0o077)
    main()
