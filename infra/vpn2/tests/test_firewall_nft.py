"""NFT block of vpn2.firewall: text generators and nftables.conf."""
import json
import os
from pathlib import Path

import jinja2
import pytest

from vpn2 import firewall

FIXTURES = Path(__file__).parent / 'fixtures' / 'firewall'
GOOG = Path(__file__).parents[2] / 'ansible/roles/zapret/files/google-prefixes.json'
HY2HOP = Path(__file__).parents[2] / 'ansible/roles/exit/templates/nftables-hy2hop.conf.j2'


def fixture(name):
    return (FIXTURES / name).read_text()


# --- generators -------------------------------------------------------------

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


def hy2hop(direct, warp, mgmt):
    """Шаблон роли exit с портами каналов и диапазонами по умолчанию."""
    env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(HY2HOP.read_text()).render(
        vpn2_env={'HY2_DIRECT_PORT': direct, 'HY2_WARP_PORT': warp, 'HY2_MGMT_PORT': mgmt},
        exit_hop_direct='20000-24999', exit_hop_warp='25000-29999', exit_hop_mgmt='30000-30999')


def test_hy2hop_table_plain_and_inside():
    plain = hy2hop('443', '8443', '30500')
    assert plain == fixture('hy2hop_plain.nft')
    assert 'udp dport 30000-30999 udp dport != 30500 redirect to :30500' in plain
    inside = hy2hop('20100', '18443', '8444')
    assert inside == fixture('hy2hop_inside.nft')
    assert 'udp dport 20000-24999 udp dport != 20100 redirect to :20100' in inside


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
