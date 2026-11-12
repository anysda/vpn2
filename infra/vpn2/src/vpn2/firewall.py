"""Firewall helpers: UFW rule reconciliation and state checks (stdlib only).

Every external call goes through the ``run`` parameter, so tests swap it for a
double. The nftables text generators follow the UFW block.
"""
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from vpn2.fsutil import atomic


class FirewallError(RuntimeError):
    """An external program returned non-zero, timed out or is missing."""

    def __init__(self, argv, returncode, stderr=''):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f'{shlex.join(self.argv)} -> {returncode}: {str(stderr).strip()}')


def run(argv, *, input=None, timeout=30):  # noqa: A002 - same name as subprocess.run
    """Run argv with captured text output, C locale; the caller checks the code."""
    env = dict(os.environ, LC_ALL='C')
    return subprocess.run(argv, input=input, capture_output=True, text=True,
                          timeout=timeout, env=env)


def _call(runner, argv, *, check=True):
    """Call runner(argv); a missing program or timeout becomes FirewallError.

    With check=True a non-zero code raises as well. Returns the result.
    """
    try:
        result = runner(argv)
    except FileNotFoundError as error:
        raise FirewallError(argv, 127, str(error)) from error
    except subprocess.TimeoutExpired as error:
        raise FirewallError(argv, 124, 'timeout') from error
    if check and result.returncode != 0:
        raise FirewallError(argv, result.returncode, result.stderr or '')
    return result


def _ok(runner, argv):
    """True if argv ran and exited 0; a missing program or timeout is False."""
    try:
        return _call(runner, argv, check=False).returncode == 0
    except FirewallError:
        return False


def rule_key(words):
    """Comparison key of a rule; `show added` expands short port rules."""
    words = list(words)
    if len(words) == 2 and '/' in words[1]:
        port, proto = words[1].rsplit('/', 1)
        return (words[0], 'from', 'any', 'to', 'any', 'port', port, 'proto', proto)
    if 'proto' in words:
        index = words.index('proto')
        return tuple(words[:index] + words[index + 2:] + words[index:index + 2])
    return tuple(words)


def ufw_reconcile(comment, rules, aliases=(), *, run=run):
    """Bring the rules owned by comment (or an alias) to exactly `rules`.

    Missing rules are added first, then stale own rules are deleted by
    specification. Foreign rules (other comment) and matching ones are left
    alone. Returns (added, deleted) as lists of rule strings. Any ufw failure
    raises FirewallError; if `show added` fails nothing is changed.
    """
    desired = [shlex.split('allow ' + rule) for rule in rules]
    wanted = {rule_key(rule) for rule in desired}
    owned = {comment, *aliases}
    shown = _call(run, ['ufw', 'show', 'added'])
    stale = []
    present = set()
    for line in shown.stdout.splitlines():
        if not line.startswith('ufw '):
            continue
        words = shlex.split(line)[1:]
        owner = None
        if 'comment' in words:
            index = words.index('comment')
            owner = words[index + 1]
            words = words[:index]
        key = rule_key(words)
        present.add(key)
        if owner in owned and key not in wanted:
            stale.append(words)
    added = []
    for rule in desired:
        key = rule_key(rule)
        if key not in present:
            _call(run, ['ufw', *rule, 'comment', comment])
            present.add(key)
            added.append(shlex.join(rule))
    deleted = []
    for rule in stale:
        # By specification, not number: both address families go, indices stay.
        _call(run, ['ufw', '--force', 'delete', *rule])
        deleted.append(shlex.join(rule))
    return added, deleted


def ufw_active(*, run=run):
    """True if `ufw status` reports 'Status: active'; no ufw means False."""
    try:
        result = _call(run, ['ufw', 'status'], check=False)
    except FirewallError:
        return False
    return 'Status: active' in result.stdout.splitlines()


def ufw_chains_lost(*, conf='/etc/ufw/ufw.conf', run=run):
    """ufw is enabled on disk but its kernel chains are gone."""
    try:
        lines = Path(conf).read_text().splitlines()
    except OSError:
        return False
    return 'ENABLED=yes' in lines and not _ok(run, ['iptables', '-S', 'ufw-user-input'])


def ufw_repair_if_lost(*, units=('docker', 'fail2ban'), conf='/etc/ufw/ufw.conf', run=run):
    """Reload ufw if its chains vanished and restart the active services.

    Returns True if a repair ran; failure raises FirewallError.
    """
    if not ufw_chains_lost(conf=conf, run=run):
        return False
    _call(run, ['ufw', 'reload'])
    for unit in units:
        if _ok(run, ['systemctl', 'is-active', '--quiet', unit]):
            _call(run, ['systemctl', 'restart', unit])
    return True


_POLICY = {'deny': 'DROP', 'allow': 'ACCEPT', 'reject': 'REJECT'}


def ufw_defaults(incoming='deny', outgoing='allow', *, path='/etc/default/ufw', run=run):
    """Set default policies, but only those the file does not already hold.

    Returns the list of directions changed.
    """
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        lines = []
    changed = []
    for direction, name, policy in (('incoming', 'INPUT', incoming), ('outgoing', 'OUTPUT', outgoing)):
        if f'DEFAULT_{name}_POLICY="{_POLICY[policy]}"' not in lines:
            _call(run, ['ufw', 'default', policy, direction])
            changed.append(direction)
    return changed


QUIC_TABLE = '''#!/usr/sbin/nft -f
# Managed by anysda-vpn2 stage 19-yt-zapret. Не редактировать руками —
# перезаписывается стадией и таймером anysda-yt-quic-refresh.timer.

table inet anysda_ytquic
delete table inet anysda_ytquic

table inet anysda_ytquic {
    set google_v4 {
        type ipv4_addr
        flags interval
        elements = { %s }
    }
    set google_v6 {
        type ipv6_addr
        flags interval
        elements = { %s }
    }

    # priority -200 — РАНЬШЕ mangle/prerouting (-150), где стоит TPROXY.
    # Иначе пакет уйдёт в sing-box до того, как мы его увидим.
    chain prerouting {
        type filter hook prerouting priority -200; policy accept;
%s
    }
}

'''


def load_google_prefixes(path):
    """Read goog.json and return (v4, v6): collapsed prefix strings, both non-empty.

    Exactly one prefix per row, strict network bits, no /0. ValueError always
    names the path.
    """
    try:
        with open(path) as stream:
            data = json.load(stream)
    except json.JSONDecodeError as error:
        raise ValueError(f'{path}: {error}') from error
    if not isinstance(data, dict) or not isinstance(data.get('prefixes'), list):
        raise ValueError(f'{path}: ожидается объект со списком prefixes')
    v4, v6 = set(), set()
    for row in data['prefixes']:
        if not isinstance(row, dict) or ('ipv4Prefix' in row) == ('ipv6Prefix' in row):
            raise ValueError(f'{path}: запись должна содержать ровно один префикс')
        key, network, target = ('ipv4Prefix', ipaddress.IPv4Network, v4) if 'ipv4Prefix' in row else (
            'ipv6Prefix', ipaddress.IPv6Network, v6)
        try:
            prefix = network(row[key], strict=True)
        except ValueError as error:
            raise ValueError(f'{path}: {error}') from error
        if prefix.prefixlen == 0:
            raise ValueError(f'{path}: маршрут по умолчанию недопустим')
        target.add(prefix)
    if not v4 or not v6:
        raise ValueError(f'{path}: требуются непустые списки IPv4 и IPv6')
    return tuple([str(prefix) for prefix in ipaddress.collapse_addresses(family)]
                 for family in (v4, v6))


def google_quic_table(v4, v6, ifaces=('wg0', 'xfrm0', 'tun0')):
    """Таблица anysda_ytquic: UDP/443 из туннелей к Google отбрасывается, клиент уходит на TCP."""
    drops = '\n'.join(f'        iifname "{iface}" udp dport 443 {family} daddr @google_{name} counter drop'
                      for iface in ifaces for family, name in (('ip', 'v4'), ('ip6', 'v6')))
    return QUIC_TABLE % (', '.join(v4), ', '.join(v6), drops)


_FLUSH_RE = re.compile(rb'(?m)^[ \t]*flush[ \t]+ruleset[ \t]*;?[ \t]*(#.*)?$')


def preserve_nftables_conf(conf='/etc/nftables.conf'):
    """Comment out ``flush ruleset`` in nftables.conf; True if the file changed.

    The first change keeps a copy ``<conf>.before-vpn2`` (never overwritten).
    A repeat finds no ``flush ruleset`` and touches nothing.
    """
    conf = Path(conf)
    if not conf.is_file():
        return False
    raw = conf.read_bytes()
    new = _FLUSH_RE.sub(b'# vpn2: preserve tables owned by ufw, routing, Docker and fail2ban', raw)
    if new == raw:
        return False
    backup = conf.with_name(conf.name + '.before-vpn2')
    if not backup.exists():
        shutil.copy2(conf, backup)
    atomic(conf, new, mode=conf.stat().st_mode & 0o7777)
    return True
