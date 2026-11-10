"""NFT block of vpn2.firewall: text generators, wrappers and the guard snapshot."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vpn2 import firewall

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
