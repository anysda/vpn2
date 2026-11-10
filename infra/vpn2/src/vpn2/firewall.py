"""Firewall helpers: UFW rule reconciliation and state checks (stdlib only).

Every external call goes through the ``run`` parameter, so tests swap it for a
double. The NFT block and GuardSnapshot follow the UFW block.
"""
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path


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


def dns_rules(destination, subnets):
    """udp and tcp :53 rules to destination for every subnet."""
    rules = []
    for subnet in subnets:
        rules.append(f'proto udp from {subnet} to {destination} port 53')
        rules.append(f'proto tcp from {subnet} to {destination} port 53')
    return rules


def ufw_allow(rule, comment=None, *, if_present=False, run=run):
    """One-off `ufw allow`; with if_present a missing ufw does nothing."""
    argv = ['ufw', 'allow', *shlex.split(rule)]
    if comment is not None:
        argv += ['comment', comment]
    try:
        _call(run, argv)
    except FirewallError as error:
        if if_present and error.returncode == 127:
            return
        raise


def ufw_delete(rule, *, missing_ok=True, run=run):
    """One-off removal of an allow rule; missing_ok hides refusal and absent ufw."""
    argv = ['ufw', '--force', 'delete', 'allow', *shlex.split(rule)]
    try:
        _call(run, argv)
    except FirewallError:
        if not missing_ok:
            raise


def ufw_active(*, run=run):
    """True if `ufw status` reports 'Status: active'; no ufw means False."""
    try:
        result = _call(run, ['ufw', 'status'], check=False)
    except FirewallError:
        return False
    return 'Status: active' in result.stdout.splitlines()


def ufw_chains_lost(*, conf='/etc/ufw/ufw.conf', require_input_jump=False, run=run):
    """ufw is enabled on disk but its kernel chains are gone.

    require_input_jump also demands `INPUT -j ufw-before-input` to exist.
    """
    try:
        lines = Path(conf).read_text().splitlines()
    except OSError:
        return False
    if 'ENABLED=yes' not in lines:
        return False
    if not _ok(run, ['iptables', '-S', 'ufw-user-input']):
        return True
    if require_input_jump and not _ok(run, ['iptables', '-C', 'INPUT', '-j', 'ufw-before-input']):
        return True
    return False


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




# --- NFT: text generators -------------------------------------------------

def own_table(table, body, *, header=(), family='inet', gap=False):
    """Text of a file that replaces one whole table: table / delete table / table {body}.

    The first two lines make the load idempotent: the table is created if
    missing, then deleted, then defined anew. ``header`` lines go first as is,
    ``body`` is a string or a list of already indented lines. ``gap`` puts an
    empty line between the delete and the definition (the YouTube QUIC file).
    """
    lines = list(header) + [f'table {family} {table}', f'delete table {family} {table}']
    if gap:
        lines.append('')
    lines.append(f'table {family} {table} {{')
    lines.extend(body.split('\n') if isinstance(body, str) else body)
    lines.append('}')
    return '\n'.join(lines) + '\n'


_TABLE_RE = re.compile(r'(?m)^\s*(?:add\s+|create\s+|delete\s+|flush\s+)?table\s+(\S+)\s+(\S+)')


def table_only(text, table, family='inet'):
    """Raise ValueError unless text touches only the table ``family table``.

    Rejects ``flush ruleset``, ``list ruleset``, a foreign table, a text with
    no table at all and a text with a foreign table among its own.
    """
    named = _TABLE_RE.findall(text)
    if re.search(r'(?m)^\s*(?:flush|list)\s+ruleset\b', text) or not named or \
            any(found != (family, table) for found in named):
        raise ValueError(f'nft: candidate may only change table {family} {table}')


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
    """The anysda_ytquic table: UDP/443 from tunnels to Google falls back to TCP.

    Byte for byte the output of gen-yt-quic-nft.py (stage 19-yt-zapret),
    including its closing empty line.
    """
    header = ['#!/usr/sbin/nft -f',
              '# Managed by anysda-vpn2 stage 19-yt-zapret. Не редактировать руками —',
              '# перезаписывается стадией и таймером anysda-yt-quic-refresh.timer.',
              '']
    body = [
        '    set google_v4 {',
        '        type ipv4_addr',
        '        flags interval',
        '        elements = { ' + ', '.join(v4) + ' }',
        '    }',
        '    set google_v6 {',
        '        type ipv6_addr',
        '        flags interval',
        '        elements = { ' + ', '.join(v6) + ' }',
        '    }',
        '',
        '    # priority -200 — РАНЬШЕ mangle/prerouting (-150), где стоит TPROXY.',
        '    # Иначе пакет уйдёт в sing-box до того, как мы его увидим.',
        '    chain prerouting {',
        '        type filter hook prerouting priority -200; policy accept;',
    ]
    for iface in ifaces:
        body.append(f'        iifname "{iface}" udp dport 443 ip daddr @google_v4 counter drop')
        body.append(f'        iifname "{iface}" udp dport 443 ip6 daddr @google_v6 counter drop')
    body.append('    }')
    return own_table('anysda_ytquic', body, header=header, gap=True) + '\n'


def hy2hop_table(ranges, listen):
    """The vpn2_hy2hop table: redirect port ranges to the channel listen ports.

    ``ranges`` maps channel -> (lo, hi), ``listen`` maps channel -> port. A
    listen port inside a range is excluded from it with ``udp dport != P``.
    """
    body = ['    chain prerouting {',
            '        type nat hook prerouting priority dstnat; policy accept;']
    for channel, (low, high) in ranges.items():
        skip = ''.join(f' udp dport != {port}' for port in listen.values() if low <= port <= high)
        body.append(f'        udp dport {low}-{high}{skip} redirect to :{listen[channel]}')
    body.append('    }')
    header = ['#!/usr/sbin/nft -f',
              '# Managed by anysda-vpn2 stage 10-foreign. Перезаписывается каждым прогоном.']
    return own_table('vpn2_hy2hop', body, header=header)


def reload_dropin(files):
    """Text of an nftables.service drop-in that reloads the given nft files.

    One file gives the plain ``if [ -f F ]`` form (10-foreign), several give
    a loop over them (19-yt-zapret).
    """
    files = list(files)
    if len(files) == 1:
        command = f"if [ -f {files[0]} ]; then /usr/sbin/nft -f {files[0]}; fi"
    else:
        command = (f'for f in {" ".join(files)}; do if [ -f "$f" ]; then '
                   '/usr/sbin/nft -f "$f"; fi; done')
    return (f"[Service]\nExecStartPost=/bin/sh -ec '{command}'\n"
            f"ExecReload=/bin/sh -ec '{command}'\n")
