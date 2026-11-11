import shlex
import subprocess
from types import SimpleNamespace

import pytest

from vpn2 import firewall
from vpn2.firewall import FirewallError

HEADER = "Added user rules (see 'ufw status' for running firewall):"
DNS = shlex.split('allow from 198.51.100.0/24 to 192.0.2.53 port 53 proto udp')

STATUS_ACTIVE = '''Status: active

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW       Anywhere
51820/udp                  ALLOW       Anywhere
22/tcp (v6)                ALLOW       Anywhere (v6)
51820/udp (v6)             ALLOW       Anywhere (v6)
'''
STATUS_INACTIVE = 'Status: inactive\n'


def done(stdout='', code=0, stderr=''):
    return SimpleNamespace(stdout=stdout, returncode=code, stderr=stderr)


class FakeUfw:
    """Persistent double of ufw: `show added` prints the stored rules."""

    def __init__(self, rules=(), fail_on=None, show_code=0):
        self.rules = [list(r) for r in rules]
        self.calls = []
        self.fail_on = fail_on
        self.show_code = show_code

    def __call__(self, argv, **_):
        self.calls.append(list(argv))
        args = argv[1:]
        if argv[0] != 'ufw':
            raise AssertionError(argv)
        if args == ['show', 'added']:
            if self.show_code:
                return done(code=self.show_code, stderr='boom')
            lines = [HEADER] + [shlex.join(['ufw', *r]) for r in self.rules]
            return done('\n'.join(lines) + '\n')
        if self.fail_on and self.fail_on(args):
            return done(code=1, stderr='ERROR: refused')
        if args[:2] == ['--force', 'delete']:
            target = args[2:]
            self.rules = [r for r in self.rules if r[:len(target)] != target]
        elif args[0] == 'allow':
            spec = args[:args.index('comment')] if 'comment' in args else args
            self.rules = [r for r in self.rules if r[:len(spec)] != spec]
            self.rules.append(args)
        else:
            raise AssertionError(argv)
        return done()

    def changing(self):
        return [c for c in self.calls if c[1:3] != ['show', 'added']]


def missing(argv, **_):
    raise FileNotFoundError(2, 'No such file', argv[0])


def test_rule_key_expands_short_and_moves_proto():
    assert firewall.rule_key(['51820/udp']) == firewall.rule_key(['51820/udp'])
    assert firewall.rule_key(['allow', '51820/udp']) == (
        'allow', 'from', 'any', 'to', 'any', 'port', '51820', 'proto', 'udp')
    long = ['allow', 'proto', 'udp', 'from', 'any', 'to', 'any', 'port', '51820']
    assert firewall.rule_key(long) == firewall.rule_key(
        ['allow', 'from', 'any', 'to', 'any', 'port', '51820', 'proto', 'udp'])
    assert firewall.rule_key(['allow', '51820/udp']) == firewall.rule_key(long)
    assert firewall.rule_key(['allow', '20000:24999/udp'])[6] == '20000:24999'
    assert firewall.rule_key([]) == ()


def test_reconcile_adds_before_deleting():
    ufw = FakeUfw([['allow', '59999/udp', 'comment', 'wireguard listener']])
    added, deleted = firewall.ufw_reconcile('wireguard listener', ['51820/udp'], run=ufw)
    assert (added, deleted) == (['allow 51820/udp'], ['allow 59999/udp'])
    assert ufw.changing() == [
        ['ufw', 'allow', '51820/udp', 'comment', 'wireguard listener'],
        ['ufw', '--force', 'delete', 'allow', '59999/udp'],
    ]


def test_reconcile_repeat_makes_no_changing_calls():
    ufw = FakeUfw([['allow', '59999/udp', 'comment', 'ssh']])
    first = firewall.ufw_reconcile('ssh', ['22/tcp'], run=ufw)
    assert first == (['allow 22/tcp'], ['allow 59999/udp'])
    ufw.calls.clear()
    assert firewall.ufw_reconcile('ssh', ['22/tcp'], run=ufw) == ([], [])
    assert ufw.changing() == []


def test_reconcile_short_and_expanded_form_are_one_rule():
    expanded = ['allow', 'from', 'any', 'to', 'any', 'port', '51820', 'proto', 'udp']
    for existing in (['allow', '51820/udp'], expanded,
                     ['allow', 'proto', 'udp', 'from', 'any', 'to', 'any', 'port', '51820']):
        ufw = FakeUfw([existing + ['comment', 'wg']])
        assert firewall.ufw_reconcile('wg', ['51820/udp'], run=ufw) == ([], [])
        assert ufw.changing() == []


def test_reconcile_leaves_foreign_rules_alone():
    manual = ['allow', '59998/udp', 'comment', 'manual wg']
    same_port = ['allow', '51820/udp', 'comment', 'manual listener']
    ufw = FakeUfw([manual, same_port, ['allow', '1/tcp']])
    assert firewall.ufw_reconcile('wg', ['51820/udp'], run=ufw) == ([], [])
    assert ufw.changing() == []
    assert manual in ufw.rules and same_port in ufw.rules


def test_reconcile_non_ascii_comment_and_alias():
    old = ['allow', '8080/tcp', 'comment', 'caddy HTTP панель']
    manual = ['allow', '8081/tcp', 'comment', 'manual caddy HTTP панель']
    ufw = FakeUfw([old, manual])
    firewall.ufw_reconcile('caddy HTTP + ACME challenge', ['80/tcp'],
                           aliases=('caddy HTTP панель',), run=ufw)
    assert old not in ufw.rules and manual in ufw.rules
    dns_old = DNS + ['comment', 'wireguard → AdGuard DNS']
    ufw = FakeUfw([dns_old, ['allow', '53/tcp', 'comment', 'manual DNS']])
    rules = firewall.dns_rules('192.0.2.53', ['203.0.113.0/24'])
    added, deleted = firewall.ufw_reconcile('wireguard → AdGuard DNS', rules, run=ufw)
    assert len(added) == 2 and len(deleted) == 1
    assert dns_old not in ufw.rules and len(ufw.rules) == 3


def test_reconcile_empty_rules_removes_all_own():
    ufw = FakeUfw([['allow', '80/tcp', 'comment', 'caddy HTTP панель'],
                   ['allow', '443/tcp', 'comment', 'caddy HTTPS панель'],
                   ['allow', '22/tcp', 'comment', 'ssh']])
    added, deleted = firewall.ufw_reconcile('caddy HTTP + ACME challenge', [],
                                            aliases=('caddy HTTP панель',), run=ufw)
    assert added == [] and deleted == ['allow 80/tcp']
    assert len(ufw.rules) == 2


def test_reconcile_port_range_and_duplicate_rule():
    ufw = FakeUfw()
    added, _ = firewall.ufw_reconcile('hop', ['20000:24999/udp', '20000:24999/udp'], run=ufw)
    assert added == ['allow 20000:24999/udp']
    assert firewall.ufw_reconcile('hop', ['20000:24999/udp'], run=ufw) == ([], [])


def test_reconcile_show_added_failure_changes_nothing():
    ufw = FakeUfw([['allow', '1/udp', 'comment', 'x']], show_code=3)
    with pytest.raises(FirewallError) as info:
        firewall.ufw_reconcile('x', ['2/udp'], run=ufw)
    assert info.value.argv == ['ufw', 'show', 'added'] and info.value.returncode == 3
    assert ufw.changing() == []


def test_reconcile_failure_midway_reports_argv_and_code():
    ufw = FakeUfw([['allow', '1/udp', 'comment', 'x']],
                  fail_on=lambda args: args[:3] == ['--force', 'delete', 'allow'])
    with pytest.raises(FirewallError) as info:
        firewall.ufw_reconcile('x', ['2/udp'], run=ufw)
    assert info.value.argv == ['ufw', '--force', 'delete', 'allow', '1/udp']
    assert info.value.returncode == 1 and 'refused' in info.value.stderr
    assert 'refused' in str(info.value)


def test_reconcile_without_ufw_raises():
    with pytest.raises(FirewallError) as info:
        firewall.ufw_reconcile('x', ['1/udp'], run=missing)
    assert info.value.returncode == 127


def test_reconcile_timeout_is_firewall_error():
    def slow(argv, **_):
        raise subprocess.TimeoutExpired(argv, 30)
    with pytest.raises(FirewallError) as info:
        firewall.ufw_reconcile('x', [], run=slow)
    assert info.value.returncode == 124


def test_dns_rules():
    assert firewall.dns_rules('192.0.2.53', []) == []
    assert firewall.dns_rules('192.0.2.53', ['198.51.100.0/24', '203.0.113.0/24']) == [
        'proto udp from 198.51.100.0/24 to 192.0.2.53 port 53',
        'proto tcp from 198.51.100.0/24 to 192.0.2.53 port 53',
        'proto udp from 203.0.113.0/24 to 192.0.2.53 port 53',
        'proto tcp from 203.0.113.0/24 to 192.0.2.53 port 53']
    ufw = FakeUfw([DNS + ['comment', 'DNS']])
    rules = firewall.dns_rules('192.0.2.53', ['198.51.100.0/24'])
    assert firewall.ufw_reconcile('DNS', rules, run=ufw) == (
        ['allow proto tcp from 198.51.100.0/24 to 192.0.2.53 port 53'], [])


def test_ufw_active():
    assert firewall.ufw_active(run=lambda *_, **__: done(STATUS_ACTIVE))
    assert not firewall.ufw_active(run=lambda *_, **__: done(STATUS_INACTIVE))
    assert not firewall.ufw_active(run=lambda *_, **__: done('', 1))
    assert not firewall.ufw_active(run=lambda *_, **__: done('Status: active (logging)\n'))
    assert not firewall.ufw_active(run=missing)


def iptables(chain=True, jump=True, calls=None):
    def fake(argv, **_):
        if calls is not None:
            calls.append(list(argv))
        if argv[:2] == ['iptables', '-S']:
            return done(code=0 if chain else 1)
        if argv[:2] == ['iptables', '-C']:
            return done(code=0 if jump else 1)
        return done()
    return fake


def test_ufw_chains_lost(tmp_path):
    conf = tmp_path / 'ufw.conf'
    conf.write_text('# c\nENABLED=yes\nLOGLEVEL=low\n')
    assert firewall.ufw_chains_lost(conf=conf, run=iptables(chain=False))
    assert not firewall.ufw_chains_lost(conf=conf, run=iptables())
    assert not firewall.ufw_chains_lost(conf=conf, run=iptables(jump=False))
    assert firewall.ufw_chains_lost(conf=conf, require_input_jump=True, run=iptables(jump=False))
    assert firewall.ufw_chains_lost(conf=conf, run=missing)
    conf.write_text('ENABLED=no\n')
    assert not firewall.ufw_chains_lost(conf=conf, run=iptables(chain=False))
    assert not firewall.ufw_chains_lost(conf=tmp_path / 'absent', run=iptables(chain=False))


def test_ufw_repair_if_lost(tmp_path):
    conf = tmp_path / 'ufw.conf'
    conf.write_text('ENABLED=yes\n')
    calls = []

    def fake(argv, **_):
        calls.append(list(argv))
        if argv[:2] == ['iptables', '-S']:
            return done(code=1)
        if argv[:3] == ['systemctl', 'is-active', '--quiet']:
            return done(code=0 if argv[3] == 'docker' else 3)
        return done()

    assert firewall.ufw_repair_if_lost(conf=conf, run=fake) is True
    assert ['ufw', 'reload'] in calls
    assert ['systemctl', 'restart', 'docker'] in calls
    assert ['systemctl', 'restart', 'fail2ban'] not in calls
    assert calls.index(['ufw', 'reload']) < calls.index(['systemctl', 'restart', 'docker'])
    calls.clear()
    assert firewall.ufw_repair_if_lost(conf=conf, run=iptables(calls=calls)) is False
    assert ['ufw', 'reload'] not in calls


def test_ufw_repair_reload_failure(tmp_path):
    conf = tmp_path / 'ufw.conf'
    conf.write_text('ENABLED=yes\n')

    def fake(argv, **_):
        return done(code=1, stderr='no') if argv[0] in ('iptables', 'ufw') else done()

    with pytest.raises(FirewallError) as info:
        firewall.ufw_repair_if_lost(units=('docker',), conf=conf, run=fake)
    assert info.value.argv == ['ufw', 'reload']


def test_ufw_defaults(tmp_path):
    path = tmp_path / 'ufw'
    path.write_text('IPV6=yes\nDEFAULT_INPUT_POLICY="DROP"\nDEFAULT_OUTPUT_POLICY="DROP"\n')
    calls = []

    def fake(argv, **_):
        calls.append(list(argv))
        return done()

    assert firewall.ufw_defaults(path=path, run=fake) == ['outgoing']
    assert calls == [['ufw', 'default', 'allow', 'outgoing']]
    calls.clear()
    path.write_text('DEFAULT_INPUT_POLICY="DROP"\nDEFAULT_OUTPUT_POLICY="ACCEPT"\n')
    assert firewall.ufw_defaults(path=path, run=fake) == []
    assert calls == []
    assert firewall.ufw_defaults('reject', path=tmp_path / 'absent', run=fake) == ['incoming', 'outgoing']
    assert calls == [['ufw', 'default', 'reject', 'incoming'], ['ufw', 'default', 'allow', 'outgoing']]


def test_default_run_sets_c_locale_and_captures():
    result = firewall.run(['sh', '-c', 'echo $LC_ALL; cat'], input='x')
    assert result.stdout == 'C\nx' and result.returncode == 0
    with pytest.raises(subprocess.TimeoutExpired):
        firewall.run(['sleep', '5'], timeout=0.05)
