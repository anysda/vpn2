#!/usr/bin/env python3
"""Apply a managed config through a candidate.

Candidate beside the target -> syntax and project checks -> journal -> rename ->
reload -> verify -> only then "last good". A failed apply restores the last
good config; the OpenVPN CRL and ccd are separate files never kept here. An
interrupted apply is finished by the next run (or `resume`).

Exit codes: 2 candidate rejected (target untouched), 3 not applied and the
previous config restored, 4 restore failed, 5 first install failed.
"""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

RESOURCES = {
    'sing-box': {'path': '/etc/sing-box/config.json', 'mode': 0o600, 'unit': 'sing-box'},
    'openvpn': {'path': '/etc/openvpn/server/server.conf', 'mode': 0o644, 'unit': 'openvpn-server@server'},
}
# The directives the openvpn role (entryvpn.OVPN_CONF) writes, in every version so far:
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


def run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, env=ENV, timeout=60, check=False)
    if result.returncode:
        last = (result.stderr or result.stdout).strip().splitlines()
        # openvpn ends its message with a hint to --help; the cause is above.
        last = [line for line in last if 'error' in line.lower()][:1] or last
        raise RuntimeError(f'{argv[0]} {argv[1] if len(argv) > 1 else ""}: '
                           f'{last[-1] if last else "rc=" + str(result.returncode)}')
    return result.stdout


class Stopped(SystemExit):
    """SIGTERM, SIGHUP or SIGINT in an install: 128+sig, after the cleanup."""


SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


@contextlib.contextmanager
def stopping():
    """A signal inside stops apply through its finally and except clauses.

    A signal the caller ignores (nohup, a command run with "&") stays ignored;
    after the first signal the rest are ignored, so the cleanup runs whole.
    """
    def stop(number, _frame):
        for each in SIGNALS:
            signal.signal(each, signal.SIG_IGN)
        raise Stopped(128 + number)

    main = threading.current_thread() is threading.main_thread()
    previous = {number: signal.signal(number, stop) for number in SIGNALS
                if signal.getsignal(number) is not signal.SIG_IGN} if main else {}
    try:
        yield
    except Stopped:
        previous = {}  # apply exits: the signals stay ignored until then
        raise
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


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
        Path(name).unlink(missing_ok=True)


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

    def stage_now(self, resource):
        journal = self.journal(resource)
        return json.loads(journal.read_text())['stage'] if journal.exists() else None

    # Project checks: what a syntax check cannot see.
    def check(self, resource, text, path):
        if resource == 'sing-box':
            # The same port on 127.0.0.1 and ::1 is two sockets (agh-doh-v4/v6).
            sockets = [(item.get('listen'), item['listen_port'], item.get('network'))
                       for item in json.loads(text).get('inbounds', []) if 'listen_port' in item]
            if len(sockets) != len(set(sockets)):
                raise ValueError('two inbounds on one port')
            self.command(['/usr/local/bin/sing-box', 'check', '-c', str(path)])
        else:
            self.check_openvpn(text, path)

    def check_openvpn(self, text, path):
        options, managed_lines = {}, []
        # openvpn drops a UTF-8 BOM at the start of the file, nowhere else, and
        # ends a line only at a newline: splitlines() also breaks at \x1c, \x85...
        for line in text.removeprefix('﻿').split('\n'):
            words = openvpn_words(line)
            if words:
                name = words[0].removeprefix('--')
                if name not in OPENVPN_DIRECTIVES:
                    raise ValueError(f'{name} is not allowed in a candidate')
                options.setdefault(name, words[1:])
                managed_lines.append(name)
        base = self.target('openvpn').parent
        # Access is enforced only while the server reads the managed CRL and ccd;
        # openvpn applies the last crl-verify or client-config-dir.
        for name, managed in (('crl-verify', 'pki/crl.pem'), ('client-config-dir', 'ccd')):
            if managed_lines.count(name) > 1:
                raise ValueError(f'{name} is given more than once')
            value = (options.get(name) or [''])[0]
            if (base / value).resolve() != (base / managed).resolve():
                raise ValueError(f'{name} must be {base / managed}')
        for name in ('ca', 'cert', 'key', 'dh', 'tls-crypt', 'crl-verify'):
            value = (options.get(name) or ['none'])[0]
            if value != 'none' and not (base / value).exists():
                raise ValueError(f'{name} {value}: no such file')
        port = (options.get('port') or ['1194'])[0]
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f'port {port}')
        # openvpn reads the whole config before it prints the cipher list and exits,
        # so a malformed directive fails here and not in the restart. An unknown
        # cipher name fails the restart, and the last good config comes back.
        self.command(['/usr/sbin/openvpn', '--cd', str(base), '--verb', '1',
                      '--config', str(path), '--show-ciphers'])

    def activate(self, resource):
        spec = RESOURCES[resource]
        self.command(['systemctl', 'restart', spec['unit']])
        self.verify(resource)
        show = ['systemctl', 'show', '--property', 'MainPID,NRestarts', spec['unit']]
        before = self.command(show)
        time.sleep(self.settle)
        self.verify(resource)
        if self.command(show) != before or 'MainPID=0' in before:
            raise RuntimeError(f'{spec["unit"]} did not stay running')

    def verify(self, resource):
        self.command(['systemctl', 'is-active', '--quiet', RESOURCES[resource]['unit']])

    def mark(self, resource):
        write(self.good(resource), self.target(resource).read_text(), 0o600)

    def install(self, resource, source):
        with stopping():
            self.resume(resource)
            candidate = self.candidate(resource)
            try:
                self.prepare(resource, source, candidate)
            except BaseException:
                # A rejection, an error or a signal anywhere in prepare(). Only a journal
                # that says "replacing" makes the next run finish the candidate.
                if self.stage_now(resource) != 'replacing':
                    candidate.unlink(missing_ok=True)
                raise
            self.finish(resource)

    def prepare(self, resource, source, candidate):
        """A checked candidate and a journal that says "replacing"."""
        target = self.target(resource)
        try:
            body = Path(source).read_text()
            write(candidate, body, RESOURCES[resource]['mode'])
            self.check(resource, body, candidate)
        except (OSError, ValueError, RuntimeError) as error:
            raise Failed(2, f'{resource}: candidate rejected, working config untouched: {error}')
        if not self.good(resource).exists() and target.exists():
            # Nothing verified yet: the running config earns "last good" first.
            try:
                self.verify(resource)
                self.mark(resource)
            except RuntimeError:
                pass
        self.stage(resource, 'replacing')

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
            body = self.good(resource).read_text()
            self.check(resource, body, self.good(resource))
            write(self.target(resource), body, RESOURCES[resource]['mode'])
            self.activate(resource)
        except (OSError, ValueError, RuntimeError) as error:
            self.stage(resource, 'restore-failed')
            raise Failed(4, f'{resource}: {reason}; restore failed: {error}')
        self.mark(resource)
        self.journal(resource).unlink()
        raise Failed(3, f'{resource}: {reason}; previous config restored')

    def resume(self, resource):
        stage = self.stage_now(resource)
        if stage in ('replacing', 'applied'):
            self.finish(resource)
        elif stage in ('restoring', 'restore-failed'):
            self.restore(resource, 'interrupted restore')
        # install-failed stays visible until the next install succeeds.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('install', 'mark', 'resume'):
        item = sub.add_parser(name)
        item.add_argument('resource', choices=sorted(RESOURCES))
        if name == 'install':
            item.add_argument('file')
    args = parser.parse_args(argv)
    applier = Applier()
    applier.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (applier.state / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if args.action == 'install':
                applier.install(args.resource, args.file)
            else:
                applier.resume(args.resource)
                if args.action == 'mark':
                    applier.verify(args.resource)
                    applier.mark(args.resource)
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
