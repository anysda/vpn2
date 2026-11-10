"""NFT block of vpn2.firewall: text generators, wrappers and the guard snapshot."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vpn2 import firewall
from vpn2.firewall import FirewallError

FIXTURES = Path(__file__).parent / 'fixtures' / 'firewall'
GOOG = Path(__file__).parents[2] / 'lib' / 'google-prefixes.json'
RANGES = {'DIRECT': (20000, 24999), 'WARP': (25000, 29999), 'MGMT': (30000, 30999)}


def fixture(name):
    return (FIXTURES / name).read_text()


def done(stdout='', code=0, stderr=''):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


# --- generators -------------------------------------------------------------

def test_own_table_shape_and_repeat():
    text = firewall.own_table('t1', ['    chain c {', '    }'], header=['# h'])
    assert text == '# h\ntable inet t1\ndelete table inet t1\ntable inet t1 {\n    chain c {\n    }\n}\n'
    assert firewall.own_table('t1', '    x', family='ip').startswith('table ip t1\ndelete table ip t1\n')
    assert firewall.own_table('t1', ['    chain c {', '    }'], header=['# h']) == text


def test_own_table_output_passes_table_only():
    firewall.table_only(firewall.own_table('anysda_x', '    set s {\n    }'), 'anysda_x')


def test_google_quic_table_matches_old_generator_byte_for_byte():
    v4, v6 = firewall.load_google_prefixes(GOOG)
    assert firewall.google_quic_table(v4, v6).encode() == (FIXTURES / 'ytquic.nft').read_bytes()


def test_google_quic_table_interfaces_and_families():
    text = firewall.google_quic_table(['192.0.2.0/24'], ['2001:db8::/32'])
    assert text.count('counter drop') == 6
    assert 'elements = { 192.0.2.0/24 }' in text and 'elements = { 2001:db8::/32 }' in text
    one = firewall.google_quic_table(['192.0.2.0/24'], ['2001:db8::/32'], ifaces=('wg7',))
    assert one.count('counter drop') == 2 and 'iifname "wg7"' in one and 'xfrm0' not in one
    assert firewall.google_quic_table(['192.0.2.0/24'], ['2001:db8::/32']) == text


def write_goog(tmp_path, rows):
    path = tmp_path / 'goog.json'
    path.write_text(json.dumps({'prefixes': rows}))
    return path


V4 = {'ipv4Prefix': '192.0.2.0/24'}
V6 = {'ipv6Prefix': '2001:db8::/32'}


def test_load_google_prefixes_collapses_overlap(tmp_path):
    rows = [V4, {'ipv4Prefix': '192.0.2.0/25'}, {'ipv4Prefix': '192.0.3.0/24'}, V6]
    assert firewall.load_google_prefixes(write_goog(tmp_path, rows)) == (['192.0.2.0/23'], ['2001:db8::/32'])


@pytest.mark.parametrize('rows', [
    [V4],                                    # only v4
    [V6],                                    # only v6
    [],                                      # nothing
    [V4, V6, {}],                            # empty record
    [V4, V6, {'ipv4Prefix': '0.0.0.0/0'}],   # default route
    [V4, V6, {'ipv6Prefix': '::/0'}],
    [V4, V6, {'ipv4Prefix': '192.0.2.5/24'}],  # host bits
    [V4, V6, {'ipv4Prefix': '192.0.2.0/24', 'ipv6Prefix': '2001:db8::/32'}],  # both keys
    [V4, V6, 'text'],
    [V4, V6, {'ipv4Prefix': 'мусор'}],
])
def test_load_google_prefixes_rejects(tmp_path, rows):
    path = write_goog(tmp_path, rows)
    with pytest.raises(ValueError, match='goog.json'):
        firewall.load_google_prefixes(path)


@pytest.mark.parametrize('body', ['not json', '[]', '{"prefixes": 5}', ''])
def test_load_google_prefixes_rejects_bad_file(tmp_path, body):
    path = tmp_path / 'goog.json'
    path.write_text(body)
    with pytest.raises(ValueError, match='goog.json'):
        firewall.load_google_prefixes(path)


def test_hy2hop_table_plain_and_inside():
    plain = firewall.hy2hop_table(RANGES, {'DIRECT': 443, 'WARP': 8443, 'MGMT': 30500})
    assert plain == fixture('hy2hop_plain.nft')
    assert 'udp dport 30000-30999 udp dport != 30500 redirect to :30500' in plain
    inside = firewall.hy2hop_table(RANGES, {'DIRECT': 20100, 'WARP': 18443, 'MGMT': 8444})
    assert inside == fixture('hy2hop_inside.nft')
    assert 'udp dport 20000-24999 udp dport != 20100 redirect to :20100' in inside
    assert firewall.hy2hop_table(RANGES, {'DIRECT': 20100, 'WARP': 18443, 'MGMT': 8444}) == inside


def test_reload_dropin_one_and_two_files():
    assert firewall.reload_dropin(['/etc/nftables-hy2hop.conf']) == fixture('dropin_one.conf')
    two = firewall.reload_dropin(['/etc/nftables-yt-zapret.conf', '/etc/nftables-yt-quic.conf'])
    assert two == fixture('dropin_two.conf')
    assert two.count('ExecReload=') == 1 and two.count('ExecStartPost=') == 1


def test_table_only_accepts_own_and_variants():
    firewall.table_only('table inet anysda_x\ndelete table inet anysda_x\ntable inet anysda_x {\n}\n', 'anysda_x')
    firewall.table_only('add table inet anysda_x\n', 'anysda_x')
    firewall.table_only('table ip anysda_x\n', 'anysda_x', family='ip')


@pytest.mark.parametrize('text', [
    'flush ruleset\ntable inet anysda_x {\n}\n',
    'table inet anysda_x\n  list ruleset\n',
    'table inet other {\n}\n',
    'table ip anysda_x {\n}\n',
    '',
    '# only a comment\nadd rule inet anysda_x c accept\n',
    'table inet anysda_x {\n}\ntable inet other {\n}\n',
    'delete table inet other\ntable inet anysda_x {\n}\n',
])
def test_table_only_rejects(text):
    with pytest.raises(ValueError, match='anysda_x'):
        firewall.table_only(text, 'anysda_x')


# --- wrappers over nft ------------------------------------------------------

class Recorder:
    """run double: answers by the first matching prefix, records every call."""

    def __init__(self, answers=(), default=None):
        self.answers = list(answers)
        self.default = default or done()
        self.calls = []

    def __call__(self, argv, input=None, **_):  # noqa: A002
        self.calls.append((list(argv), input))
        for prefix, reply in self.answers:
            if list(argv[:len(prefix)]) == prefix:
                if isinstance(reply, BaseException):
                    raise reply
                return reply
        return self.default

    def argvs(self):
        return [argv for argv, _ in self.calls]


def test_nft_load_text_goes_through_stdin_and_path_as_file(tmp_path):
    run = Recorder()
    firewall.nft_load('table inet x\n', run=run)
    firewall.nft_load(tmp_path / 'a.nft', run=run)
    firewall.nft_check('table inet x\n', run=run)
    firewall.nft_check(str(tmp_path / 'b.nft'), run=run)
    assert run.calls == [
        (['nft', '-f', '-'], 'table inet x\n'),
        (['nft', '-f', str(tmp_path / 'a.nft')], None),
        (['nft', '-c', '-f', '-'], 'table inet x\n'),
        (['nft', '-c', '-f', str(tmp_path / 'b.nft')], None),
    ]


@pytest.mark.parametrize('fail', [done(code=1, stderr='syntax error'), FileNotFoundError('nft'),
                                  subprocess.TimeoutExpired('nft', 30)])
@pytest.mark.parametrize('call', [firewall.nft_check, firewall.nft_load])
@pytest.mark.parametrize('source', ['table inet x\n', Path('/nonexistent/a.nft')])
def test_nft_check_and_load_failure_raises(call, source, fail):
    run = Recorder(default=fail) if isinstance(fail, SimpleNamespace) else Recorder([(['nft'], fail)])
    with pytest.raises(FirewallError) as info:
        call(source, run=run)
    assert info.value.argv[0] == 'nft'


def test_check_refusal_means_load_is_not_called_by_the_caller():
    run = Recorder([(['nft', '-c'], done(code=1, stderr='Error: syntax'))])
    text = 'table inet x\n'
    with pytest.raises(FirewallError, match='syntax'):
        firewall.nft_check(text, run=run)
        firewall.nft_load(text, run=run)
    assert run.argvs() == [['nft', '-c', '-f', '-']]


def test_nft_list_table():
    run = Recorder([(['nft', 'list', 'table', 'inet', 'a'], done('table inet a {\n}\n'))],
                   default=done(code=1, stderr='No such file or directory'))
    assert firewall.nft_list_table('a', run=run) == 'table inet a {\n}\n'
    assert firewall.nft_list_table('gone', run=run) is None
    assert firewall.nft_list_table('a', run=Recorder([(['nft'], FileNotFoundError('nft'))])) is None
    assert firewall.nft_list_table('a', 'ip', run=run) is None
    assert run.argvs()[-1] == ['nft', 'list', 'table', 'ip', 'a']


def test_nft_delete_table_never_raises():
    assert firewall.nft_delete_table('a', run=Recorder()) is True
    assert firewall.nft_delete_table('a', run=Recorder(default=done(code=1))) is False
    assert firewall.nft_delete_table('a', run=Recorder([(['nft'], FileNotFoundError('nft'))])) is False
    assert firewall.nft_delete_table('a', run=Recorder([(['nft'], subprocess.TimeoutExpired('nft', 1))])) is False
    run = Recorder()
    firewall.nft_delete_table('a', 'ip', run=run)
    assert run.argvs() == [['nft', 'delete', 'table', 'ip', 'a']]


def ruleset(*rules):
    return json.dumps({'nftables': [{'metainfo': {}}, {'table': {'name': 'ytzapret'}}]
                       + [{'rule': {'expr': list(expr)}} for expr in rules]})


def counted(packets, queue):
    return [{'match': {}}, {'counter': {'packets': packets, 'bytes': 1}}, {'queue': {'num': queue}}]


def test_queued_packets_sums_counters_of_queue_rules():
    out = ruleset(counted(5, 200), counted(7, 200), counted(100, 201),
                  [{'counter': {'packets': 9, 'bytes': 1}}, {'accept': None}])
    run = Recorder(default=done(out))
    assert firewall.queued_packets('ytzapret', 200, run=run) == 12
    assert firewall.queued_packets('ytzapret', 201, run=run) == 100
    assert run.argvs()[0] == ['nft', '-j', 'list', 'table', 'inet', 'ytzapret']


@pytest.mark.parametrize('out', [
    ruleset(),
    ruleset([{'queue': {'num': 200}}]),         # queue rule without a counter
    ruleset(counted(3, 201)),                   # other queue
    '{"nothing": 1}', 'not json', '',
])
def test_queued_packets_without_counters_is_lookup_error(out):
    with pytest.raises(LookupError):
        firewall.queued_packets('ytzapret', 200, run=Recorder(default=done(out)))


def test_queued_packets_nft_failure_is_firewall_error():
    with pytest.raises(FirewallError):
        firewall.queued_packets('ytzapret', 200, run=Recorder(default=done(code=1, stderr='no table')))


# --- nftables.conf ----------------------------------------------------------

CONF = '#!/usr/sbin/nft -f\n\nflush ruleset   # drop all\n  flush ruleset;\ninclude "/etc/nftables.d/*.nft"\n'


def test_preserve_nftables_conf_once_and_idempotent(tmp_path):
    conf = tmp_path / 'nftables.conf'
    conf.write_text(CONF)
    os.chmod(conf, 0o640)
    assert firewall.preserve_nftables_conf(conf) is True
    marker = '# vpn2: preserve tables owned by ufw, routing, Docker and fail2ban'
    assert conf.read_text() == f'#!/usr/sbin/nft -f\n\n{marker}\n{marker}\ninclude "/etc/nftables.d/*.nft"\n'
    assert conf.stat().st_mode & 0o777 == 0o640
    backup = tmp_path / 'nftables.conf.before-vpn2'
    assert backup.read_text() == CONF
    migrated = conf.read_bytes()
    backup.write_text('manual copy')
    assert firewall.preserve_nftables_conf(conf) is False
    assert conf.read_bytes() == migrated and backup.read_text() == 'manual copy'


def test_preserve_nftables_conf_leaves_other_files_alone(tmp_path):
    assert firewall.preserve_nftables_conf(tmp_path / 'absent.conf') is False
    conf = tmp_path / 'nftables.conf'
    conf.write_text('# flush ruleset\ntable inet filter { }\nflush ruleset foo\n')
    assert firewall.preserve_nftables_conf(conf) is False
    assert not (tmp_path / 'nftables.conf.before-vpn2').exists()
    conf.write_text('')
    assert firewall.preserve_nftables_conf(conf) is False


# --- GuardSnapshot ----------------------------------------------------------

class FakeHost:
    """ufw and nft double with state: tables dict, ufw active flag, call log."""

    def __init__(self, ufw=True, active=True, tables=None, nft=True, fail=()):
        self.ufw, self.active, self.nft = ufw, active, nft
        self.tables = dict(tables or {})
        self.fail = set(fail)
        self.log = []

    def __call__(self, argv, **_):
        argv = list(argv)
        self.log.append(' '.join(argv))
        if argv[0] == 'ufw':
            if not self.ufw:
                raise FileNotFoundError('ufw')
            if ' '.join(argv[1:]) in self.fail:
                return done(code=1, stderr='ufw failed')
            if argv[1] == 'status':
                return done('Status: active\n' if self.active else 'Status: inactive\n')
            if argv[1:] == ['--force', 'enable']:
                self.active = True
            if argv[1:] == ['--force', 'disable']:
                self.active = False
            return done()
        if not self.nft:
            raise FileNotFoundError('nft')
        if argv[1:3] == ['list', 'table']:
            name = argv[4]
            return done(self.tables[name]) if name in self.tables else done(code=1)
        if argv[1] == 'delete':
            return done() if self.tables.pop(argv[4], None) is not None else done(code=1)
        if argv[1] == '-f':
            if 'nft -f' in self.fail:
                return done(code=1, stderr='nft refused')
            text = Path(argv[2]).read_text()
            self.tables[text.split()[2]] = text
            return done()
        raise AssertionError(argv)


def make_etc(tmp_path):
    etc = tmp_path / 'etc'
    (etc / 'ufw' / 'applications.d').mkdir(parents=True)
    (etc / 'default').mkdir()
    (etc / 'ufw' / 'user.rules').write_bytes(b'### RULES ###\n-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n')
    (etc / 'ufw' / 'applications.d' / 'панель-→.profile').write_bytes('[Панель]\nports=8443/tcp\n'.encode())
    os.chmod(etc / 'ufw' / 'user.rules', 0o640)
    (etc / 'default' / 'ufw').write_text('DEFAULT_INPUT_POLICY="DROP"\n')
    return etc


def tree(root):
    return {str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None, path.stat().st_mode)
            for path in sorted(root.rglob('*'))}


TABLES = ['inet ytzapret', 'inet anysda_ytquic']
T1 = 'table inet ytzapret {\n    chain c {\n    }\n}\n'


def test_guard_snapshot_roundtrip_is_byte_exact(tmp_path):
    etc = make_etc(tmp_path)
    before = tree(etc)
    host = FakeHost(tables={'ytzapret': T1})
    guard = firewall.GuardSnapshot.take(tmp_path / 'guard', TABLES, etc, run=host)
    (etc / 'ufw' / 'user.rules').write_text('broken\n')
    (etc / 'ufw' / 'extra').write_text('new')
    (etc / 'default' / 'ufw').write_text('changed\n')
    host.active, host.tables['ytzapret'] = False, 'table inet ytzapret {\n}\n'
    host.tables['anysda_ytquic'] = 'table inet anysda_ytquic {\n}\n'
    assert guard.restore() is True
    assert tree(etc) == before
    assert host.active is True
    assert host.tables == {'ytzapret': T1}
    assert 'ufw --force enable' in host.log and 'ufw reload' in host.log
    assert not (etc / 'ufw.vpn2-guard').exists()


def test_guard_restore_repeats_with_the_same_result(tmp_path):
    etc = make_etc(tmp_path)
    before = tree(etc)
    host = FakeHost(tables={'ytzapret': T1})
    guard = firewall.GuardSnapshot.take(tmp_path / 'guard', TABLES, etc, run=host)
    assert guard.restore() and guard.restore()
    assert tree(etc) == before and host.tables == {'ytzapret': T1}


def test_guard_ufw_was_inactive_restore_disables(tmp_path):
    etc = make_etc(tmp_path)
    host = FakeHost(active=False)
    guard = firewall.GuardSnapshot.take(tmp_path / 'guard', TABLES, etc, run=host)
    host.active = True
    assert guard.restore() is True
    assert host.active is False
    assert 'ufw reload' not in host.log and 'ufw --force disable' in host.log


def test_guard_table_was_absent_restore_only_deletes(tmp_path):
    etc = make_etc(tmp_path)
    host = FakeHost()
    guard = firewall.GuardSnapshot.take(tmp_path / 'guard', TABLES, etc, run=host)
    assert not list((tmp_path / 'guard' / 'snap').glob('nft-*'))
    host.tables['ytzapret'] = T1
    host.log.clear()
    assert guard.restore() is True
    assert host.tables == {}
    assert not any(line.startswith('nft -f') for line in host.log)


def test_guard_without_ufw_marks_and_restore_skips_ufw(tmp_path):
    etc = tmp_path / 'etc'
    etc.mkdir()
    host = FakeHost(ufw=False, tables={'ytzapret': T1})
    guard = firewall.GuardSnapshot.take(tmp_path / 'guard', TABLES, etc, run=host)
    assert (tmp_path / 'guard' / 'snap' / 'no-ufw').exists()
    host.tables.clear()
    assert guard.restore() is True
    assert host.tables == {'ytzapret': T1}
    assert not (etc / 'ufw').exists()


def test_guard_restore_nft_refusal_returns_false_not_raises(tmp_path):
    etc = make_etc(tmp_path)
    host = FakeHost(tables={'ytzapret': T1, 'anysda_ytquic': 'table inet anysda_ytquic {\n}\n'})
    guard = firewall.GuardSnapshot.take(tmp_path / 'guard', TABLES, etc, run=host)
    host.fail = {'nft -f'}
    assert guard.restore() is False
    # a failure does not stop the rest: both tables were tried, ufw was restored
    assert sum(line.startswith('nft -f') for line in host.log) == 2
    assert 'ufw reload' in host.log


def test_guard_restore_ufw_refusal_returns_false_and_goes_on(tmp_path):
    etc = make_etc(tmp_path)
    host = FakeHost(tables={'ytzapret': T1})
    guard = firewall.GuardSnapshot.take(tmp_path / 'guard', TABLES, etc, run=host)
    host.tables.clear()
    host.fail = {'--force enable'}
    assert guard.restore() is False
    assert host.tables == {'ytzapret': T1}
    assert 'ufw reload' not in host.log


def test_guard_restore_timeout_and_missing_nft(tmp_path):
    etc = make_etc(tmp_path)
    host = FakeHost(tables={'ytzapret': T1})
    guard = firewall.GuardSnapshot.take(tmp_path / 'guard', TABLES, etc, run=host)

    def slow(argv, **_):
        if argv[0] == 'ufw':
            raise subprocess.TimeoutExpired(argv, 30)
        return host(argv)
    guard.run = slow
    assert guard.restore() is False
    guard.run = FakeHost(nft=False)
    assert guard.restore() is True       # no nft: skipped, like command -v


def test_guard_take_ufw_status_timeout_propagates(tmp_path):
    etc = make_etc(tmp_path)

    def slow(argv, **_):
        raise subprocess.TimeoutExpired(argv, 30)
    with pytest.raises(FirewallError):
        firewall.GuardSnapshot.take(tmp_path / 'guard', TABLES, etc, run=slow)


def test_guard_take_twice_replaces_the_snapshot(tmp_path):
    etc = make_etc(tmp_path)
    host = FakeHost(tables={'ytzapret': T1})
    firewall.GuardSnapshot.take(tmp_path / 'guard', TABLES, etc, run=host)
    host.tables.clear()
    guard = firewall.GuardSnapshot.take(tmp_path / 'guard', [('inet', 'ytzapret')], etc, run=host)
    assert not list((tmp_path / 'guard' / 'snap').glob('nft-*'))
    assert guard.restore() is True
