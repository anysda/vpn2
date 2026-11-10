"""Firewall helpers: UFW rule reconciliation and state checks (stdlib only).

Every external call goes through the ``run`` parameter, so tests swap it for a
double. The NFT block is added by the next step at the end of this module.
"""
import os
import shlex
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


# NFT block and GuardSnapshot are appended below by the next step.
