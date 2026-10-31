"""wg0.conf outside apply: AllowedIPs read as networks, an emptied peer, an address on two peers.

wg parses each AllowedIPs entry into a network (no mask is /32 or /128, `/032`
is 32, `fd66:66:0::3` is `fd66:66::3`), `-X` removes that network, a peer may
end with no addresses, and an address added to a later peer leaves the earlier
one. An entry wg cannot parse rejects the whole file. The backup check,
verify-traffic.py and the `wg syncconf` body of restore_wg_apply must end with
the sets wg ends with, or fail like wg does.
"""
import ipaddress
from pathlib import Path
import re
import tempfile
import unittest

from test_wg_conf_allowedips_prefix import (
    A4, A6, B4, GONE, NEW, OLD, PASSED_GATE, PEER, PSK_GONE, PSK_NEW, PSK_ONE, PSK_TWO, SERVER, VT, WG_APPLY,
    cut, source)
from wg_root import as_root

A4_BARE = '10.66.66.3'
G4, G6 = '10.66.66.4/32', 'fd66:66::4/128'
# AllowedIPs lines of GONE (second peer) and OLD (last peer) in each form.
FORMS = {
    # 1 the address in another form than the one it was added in
    'nomask-minus': ([f'{G4}, {G6}'], [f'{A4}, {B4}, {A6}', '-10.66.66.9']),
    'mask-zero-minus': ([f'{G4}, {G6}'], [f'{A4}, {B4}, {A6}', '-10.66.66.9/032']),
    'v6-form-minus': ([f'{G4}, {G6}'], [f'{A4}, {A6}', '-fd66:66:0::3/128']),
    'nomask-add': ([f'{G4}, {G6}'], [f'{A4_BARE}, {A6}']),
    'v6-form-twice': ([f'{G4}, {G6}'], [f'{A4}, {A6}, fd66:66:0::3/128']),
    'only-removed': ([f'{G4}, {G6}'], [A4, f'-{A4_BARE}']),
    # 2 the peer ends with no addresses
    'removed-all': ([f'{G4}, {G6}'], [A4, f'-{A4}']),
    'no-allowedips': ([f'{G4}, {G6}'], []),
    # 3 the address on two peers: wg leaves it with the last one
    'two-peers': ([f'{G4}, {G6}, {A4}'], [f'{A4}, {A6}']),
    # 5 the file as written
    'canonical': ([f'{G4}, {G6}'], [f'{A4}, {A6}']),
}
# 4 wg rejects the whole file: the form and what the error must name.
REJECTED = {
    'sign-then-hash': ([f'{G4}, {G6}'], [f'{A4}, -#{A4}'], 'AllowedIPs'),
    'leading-zero-minus': ([f'{G4}, {G6}'], [A4, '-10.66.66.03/32'], '10.66.66.03'),
}
# `wg setconf` of each form on a fresh wg0 in a netns, then `wg show wg0 allowed-ips`
# (Linux 7.0, wireguard-tools 1.0.20250521): {peer: set}.
WG_ORACLE = {
    'nomask-minus': {GONE: {G4, G6}, OLD: {A4, A6}},
    'mask-zero-minus': {GONE: {G4, G6}, OLD: {A4, A6}},
    'v6-form-minus': {GONE: {G4, G6}, OLD: {A4}},
    'nomask-add': {GONE: {G4, G6}, OLD: {A4, A6}},
    'v6-form-twice': {GONE: {G4, G6}, OLD: {A4, A6}},
    'only-removed': {GONE: {G4, G6}, OLD: set()},
    'removed-all': {GONE: {G4, G6}, OLD: set()},
    'no-allowedips': {GONE: {G4, G6}, OLD: set()},
    'two-peers': {GONE: {G4, G6}, OLD: {A4, A6}},
    'canonical': {GONE: {G4, G6}, OLD: {A4, A6}},
}
SPLIT = [(VT.UNKNOWN, 'пиры wg0 расходятся с wg0.conf - полный набор для wg.apply неизвестен')]


def conf(form):
    """As 28-wireguard writes the header and backend appends the peers; GONE and OLD get the form's lines."""
    gone, old = (FORMS | {name: value[:2] for name, value in REJECTED.items()})[form]
    text = ('# Initial config — panel will overwrite via syncWireguardConfig().\n[Interface]\n'
            f'Address = 10.66.66.1/24, fd66:66::1/64\nListenPort = 51820\nPrivateKey = {SERVER}\n')
    for key, psk, lines in ((PEER, PSK_ONE, ['10.66.66.2/32, fd66:66::2/128']), (GONE, PSK_GONE, gone),
                            (OLD, PSK_TWO, old)):
        text += (f'\n[Peer]\nPublicKey = {key}\nPresharedKey = {psk}\n' +
                 ''.join(f'AllowedIPs = {line}\n' for line in lines))
    return text


def network(entry):
    """config.c parse_ip_prefix + the kernel trie: the network wg keeps for one entry, or ValueError."""
    address, slash, mask = entry.partition('/')
    if ':' in address:
        ip = ipaddress.IPv6Address(address)
    elif re.fullmatch(r'(0|[1-9]\d{0,2})(\.(0|[1-9]\d{0,2})){3}', address):
        ip = ipaddress.IPv4Address(address)
    else:
        raise ValueError(f'Unable to parse IP address: `{address}`')
    if slash and not re.fullmatch(r'\d+', mask):
        raise ValueError(f'Unable to parse mask: `{mask}`')
    return str(ipaddress.ip_network(f'{ip}/{int(mask) if slash else ip.max_prefixlen}', strict=False))


def wg_sets(text):
    """`wg setconf` then `wg show allowed-ips`: {peer: set of networks}; ValueError where wg rejects the file."""
    owners, section, peer = {}, None, None
    sets = {}
    for raw in text.split('\n'):
        line = re.sub(r'[ \t\n\v\f\r]', '', raw.split('#', 1)[0])
        if not line:
            continue
        if line.encode().lower() in (b'[interface]', b'[peer]'):
            section, peer = line.lower(), None
            continue
        name, _, value = line.partition('=')
        name = name.encode().lower().decode()
        if section != '[peer]':
            continue
        if name == 'publickey':
            peer = value
            sets[peer] = set()
        elif name == 'allowedips':
            if not value:
                raise ValueError(f'Line unrecognized: `{line}`')
            for entry in filter(None, value.split(',')):
                removing = entry[0] == '-'
                net = network(entry[1:] if entry[0] in '+-' else entry)
                if removing:
                    if owners.get(net) == peer:
                        del owners[net]
                else:
                    owners[net] = peer
    for net, peer in owners.items():
        sets[peer].add(net)
    return sets


def backup_check(text):
    """anysda-backup.sh --check-bundle: (peer addresses it reads, complaints about WireGuard peers)."""
    block = cut(source('infra/lib/anysda-backup.sh'), "wg = root / 'etc-wireguard/wg0.conf'", '\nswan = root')
    rows = [(1, True, (PEER, PSK_ONE, '10.66.66.2')),
            (2, None, (NEW, PSK_NEW, '10.66.66.3')),  # reissued: old key still on its address
            (3, True, (GONE, PSK_GONE, '10.66.66.4'))]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'etc-wireguard').mkdir()
        (root / 'etc-wireguard/wg0.conf').write_text(text)
        scope = {'re': re, 'root': root, 'bad': [], 'retired_keys': set(), 'label': lambda d: f'устройство {d}',
                 'device_columns': {'wg_public_key', 'wg_preshared_key', 'wg_ip'},
                 'devices': lambda *_fields: iter(rows)}
        exec(block, scope)
    return {key: set(value) for key, value in scope['addresses'].items()}, scope['bad']


def apply_gate(text):
    """verify-traffic.py wg_apply_check() up to the free address, live wg0 holding the conf's peers."""
    notes = []

    class Report:
        @staticmethod
        def add(_label, _component, status, note):
            notes.append((status, note))

    class Done:
        stdout = f'{PEER} {GONE} {OLD}'

    def run(*args, **_kw):
        assert args == ('wg', 'show', 'wg0', 'peers'), args
        return Done

    saved = VT.WG_CONF, VT.CONTROL_SOCKET, VT.CONTROL_WG_STATE, VT.run, VT.WireGuard.free_addresses
    with tempfile.TemporaryDirectory() as directory:
        VT.WG_CONF, VT.CONTROL_SOCKET = Path(directory) / 'wg0.conf', Path(directory)
        VT.CONTROL_WG_STATE = Path(directory) / 'absent.json'
        VT.WG_CONF.write_text(text)
        VT.run = run
        VT.WireGuard.free_addresses = staticmethod(lambda _count: [])
        try:
            VT.wg_apply_check(Report)
        finally:
            VT.WG_CONF, VT.CONTROL_SOCKET, VT.CONTROL_WG_STATE, VT.run = saved[:4]
            VT.WireGuard.free_addresses = staticmethod(saved[4])
    return notes


def syncconf_body(text):
    """verify-traffic.py restore_wg_apply(): the text it sends to `wg syncconf wg0 /dev/stdin`."""
    sent = []
    saved = VT.WORK, VT.run, VT.restore_file
    with tempfile.TemporaryDirectory() as directory:
        VT.WORK = Path(directory)
        (VT.WORK / 'wg0.conf').write_text(text)
        VT.run = lambda *args, data=None, **_kw: sent.append((args, data))
        VT.restore_file = lambda *_args: None
        try:
            VT.restore_wg_apply()
        finally:
            VT.WORK, VT.run, VT.restore_file = saved
    [(args, data)] = sent
    assert args[:3] == ('wg', 'syncconf', 'wg0'), args
    return data


@as_root
class WgConfAllowedIpsNetTests(unittest.TestCase):
    """1 address forms, 2 emptied peer, 3 address on two peers, 4 file wg rejects, 5 the file as written."""

    def test_0_reference_matches_wg(self):
        for form, wanted in WG_ORACLE.items():
            with self.subTest(form):
                sets = wg_sets(conf(form))
                self.assertEqual({GONE: sets[GONE], OLD: sets[OLD]}, wanted)
                self.assertEqual(sets[PEER], {'10.66.66.2/32', 'fd66:66::2/128'})
        for form in REJECTED:
            with self.subTest(form):
                self.assertRaises(ValueError, wg_sets, conf(form))

    def assert_reads_like_wg(self, form):
        text, wanted = conf(form), wg_sets(conf(form))
        addresses, bad = backup_check(text)
        with self.subTest('backup addresses', form=form):
            self.assertEqual(addresses, wanted)
        with self.subTest('backup complaints', form=form):
            # NEW was reissued from 10.66.66.3: OLD counts as its peer only while wg gives OLD that address.
            self.assertEqual(bad, [] if A4 in wanted[OLD] else [f'пир WireGuard {OLD}: устройства нет в БД'])
        with self.subTest('verify conf_peers', form=form):
            self.assertEqual({p['key']: sorted(p['allowed']) for p in VT.conf_peers(text)},
                             {key: sorted(value) for key, value in wanted.items()})
        expressible = all(len([a for a in value if a.endswith('/32')]) == 1 for value in wanted.values())
        with self.subTest('verify apply_peers', form=form):
            self.assertEqual(VT.apply_peers(text), WG_APPLY if expressible else None)
        with self.subTest('verify peers vs wg0', form=form):
            self.assertEqual(apply_gate(text), PASSED_GATE if expressible else SPLIT)
        body = syncconf_body(text)
        with self.subTest('syncconf body has no empty AllowedIPs', form=form):
            self.assertEqual([line for line in body.split('\n') if re.fullmatch(r'\s*AllowedIPs\s*=\s*', line)], [])
        with self.subTest('syncconf body sets', form=form):
            try:
                self.assertEqual(wg_sets(body), wanted)
            except ValueError as error:
                self.fail(f'wg rejects the body: {error}')

    def assert_fails_like_wg(self, form):
        text, named = conf(form), REJECTED[form][2]
        try:
            addresses, bad = backup_check(text)
        except Exception as error:
            with self.subTest('backup fails', form=form):
                self.assertIn(named, str(error))
        else:
            with self.subTest('backup fails', form=form):
                self.assertTrue([b for b in bad if named in b], (addresses, bad))
        for name, call in (('conf_peers', VT.conf_peers), ('apply_peers', VT.apply_peers)):
            with self.subTest(f'verify {name} fails', form=form):
                with self.assertRaises(Exception) as caught:
                    call(text)
                self.assertIn(named, str(caught.exception))
        with self.subTest('verify peers vs wg0', form=form):
            try:
                notes = apply_gate(text)
            except Exception as error:
                self.assertIn(named, str(error))
            else:
                self.assertTrue(notes and notes != PASSED_GATE and named in notes[0][1], notes)

    def test_1_address_compared_as_network(self):
        for form in ('nomask-minus', 'mask-zero-minus', 'v6-form-minus', 'nomask-add', 'v6-form-twice',
                     'only-removed'):
            self.assert_reads_like_wg(form)

    def test_2_emptied_peer_syncs_with_no_addresses(self):
        for form in ('only-removed', 'removed-all', 'no-allowedips'):
            self.assert_reads_like_wg(form)

    def test_3_address_on_two_peers_stays_with_the_last(self):
        self.assert_reads_like_wg('two-peers')

    def test_4_file_wg_rejects_is_not_read_silently(self):
        for form in REJECTED:
            self.assert_fails_like_wg(form)

    def test_5_file_as_written_reads_as_before(self):
        self.assert_reads_like_wg('canonical')


if __name__ == '__main__':
    unittest.main()
