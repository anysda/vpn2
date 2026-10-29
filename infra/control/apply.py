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
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
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


def run(argv, data=None):
    result = subprocess.run(argv, input=data, capture_output=True, text=True, timeout=60,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    if result.returncode:
        last = (result.stderr or result.stdout).strip().splitlines()
        # nft ends its message with the source line and a caret, openvpn with a
        # hint to --help; the cause is above.
        last = [line for line in last if 'error' in line.lower()][:1] or last
        raise RuntimeError(f'{argv[0]} {argv[1] if len(argv) > 1 else ""}: '
                           f'{last[-1] if last else "rc=" + str(result.returncode)}')
    return result.stdout


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
        if os.path.exists(name):
            os.unlink(name)


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


def wg_split(text):
    """Deployment-owned [Interface] section and the access-owned [Peer] tail."""
    parts = re.split(r'(?m)^(?=\s*\[Peer\]\s*$)', text, maxsplit=1)
    return parts[0].rstrip() + '\n', parts[1] if len(parts) > 1 else ''


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
            options, ciphers, managed_lines = {}, [], []
            # openvpn drops a UTF-8 BOM at the start of the file, nowhere else.
            for line in text.removeprefix('\ufeff').splitlines():
                words = openvpn_words(line)
                if words:
                    name = words[0].removeprefix('--')
                    if name not in OPENVPN_DIRECTIVES:
                        raise ValueError(f'{name} is not allowed in a candidate')
                    options.setdefault(name, words[1:])
                    # openvpn applies the last crl-verify or client-config-dir.
                    if name in ('crl-verify', 'client-config-dir'):
                        managed_lines.append(name)
                    if name in ('cipher', 'auth'):
                        ciphers += [(name, [word]) for word in words[1:]]
                    if name == 'data-ciphers':
                        ciphers += [(name, word.split(':')) for word in words[1:]]
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
                self.check_openvpn_names(ciphers)
        elif resource == 'ikev2':
            if text.count('{') != text.count('}') or not re.search(r'(?m)^\s*anysda-ikev2\s*\{', text):
                raise ValueError('connection anysda-ikev2 missing or braces unbalanced')
        elif resource == 'wg':
            header, peers = wg_split(text)
            if not re.search(rf'(?m)^\s*PrivateKey\s*=\s*{KEY}\s*$', header):
                raise ValueError('[Interface] PrivateKey missing')
            port = re.search(r'(?m)^\s*ListenPort\s*=\s*(\d+)\s*$', header)
            if not port or not 0 < int(port[1]) < 65536:
                raise ValueError('[Interface] ListenPort missing')
            if peers.count('[Peer]') != len(re.findall(rf'(?m)^\s*PublicKey\s*=\s*{KEY}\s*$', peers)):
                raise ValueError('[Peer] without a valid PublicKey')
        if spec.get('check'):
            self.command(spec['check'] + [str(path)])

    def check_openvpn_names(self, ciphers):
        with tempfile.TemporaryDirectory() as scratch:
            key = str(Path(scratch) / 'static.key')
            self.command(['/usr/sbin/openvpn', '--genkey', 'secret', key])
            for option, names in ciphers:
                # Only data-ciphers takes "?NAME": openvpn skips it when unknown, but
                # fails the start when no known cipher is left in the list.
                known = [self.openvpn_accepts(key, option, name.removeprefix('?')) for name in names]
                kind = 'digest' if option == 'auth' else 'cipher'
                for name, accepted in zip(names, known):
                    if name and not accepted and not (option == 'data-ciphers' and name.startswith('?')):
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
        # cipher ignores --auth, so a digest is tried with a CBC cipher.
        if not name:
            return False
        argv = {'cipher': ['--cipher', name],
                'data-ciphers': ['--cipher', name, '--data-ciphers', name],
                'auth': ['--cipher', 'AES-256-CBC', '--auth', name]}[option]
        try:
            self.command(['/usr/sbin/openvpn', '--verb', '1', '--test-crypto', '--secret', key] + argv)
        except RuntimeError:
            return False
        return True

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
            interface = [line for line in header.splitlines()
                         if re.match(r'^\s*(\[Interface\]\s*$|(PrivateKey|ListenPort|FwMark)\s*=)', line)]
            body = '\n'.join(interface) + '\n' + '\n'.join(
                line for line in peers.splitlines()
                if re.match(r'^\s*(\[Peer\]\s*$|(PublicKey|PresharedKey|AllowedIPs)\s*=)', line))
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
        self.resume(resource)
        spec = RESOURCES[resource]
        target = self.target(resource)
        candidate = self.candidate(resource)
        try:
            body = self.with_access(resource, Path(source).read_text())
            write(candidate, body, spec['mode'])
            self.check(resource, body, candidate)
        except (OSError, ValueError, RuntimeError) as error:
            candidate.unlink(missing_ok=True)
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
            self.check(resource, body, self.good(resource))
            write(self.target(resource), body, RESOURCES[resource]['mode'])
            self.activate(resource)
        except (OSError, ValueError, RuntimeError) as error:
            self.stage(resource, 'restore-failed')
            raise Failed(4, f'{resource}: {reason}; restore failed: {error}')
        self.mark(resource)
        self.journal(resource).unlink()
        raise Failed(3, f'{resource}: {reason}; previous config restored')

    def rollback(self, resource):
        self.resume(resource)
        self.restore(resource, 'rollback requested')

    def resume(self, resource):
        if not self.journal(resource).exists():
            return
        stage = json.loads(self.journal(resource).read_text())['stage']
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
