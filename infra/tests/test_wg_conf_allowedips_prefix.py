"""wg0.conf outside apply: AllowedIPs entries prefixed with `+` or `-`.

wg (wireguard-tools config.c, parse_ip_prefix) reads `-X` as removing X from
the peer and `+X` as adding it, entry after entry and line after line. The
backup check and verify-traffic.py take the peer's addresses from wg0.conf, so
they must end with the set wg ends with. Each reader is cut out of its script
and run on one wg0.conf whose last peer carries the prefixed lines.

The sign is where wg versions part: wireguard-tools 1.0.20210914 (Ubuntu 24.04)
cannot parse `+X` or `-X` and rejects the file, 1.0.20250521 (the nodes, Ubuntu
26.04) applies them. The expectation is the decision of the wg installed here
(installed_wg); where it rejects, every reader must refuse naming the entry wg
names, as wg does on that node.
"""
import base64
import importlib.util
import os
from pathlib import Path
import re
import tempfile
import unittest
from wg_root import as_root

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))

SERVER = base64.b64encode(bytes(range(32))).decode()
PEER = base64.b64encode(bytes(range(64, 96))).decode()
GONE = base64.b64encode(bytes(range(96, 128))).decode()
OLD = base64.b64encode(bytes(range(128, 160))).decode()
NEW = base64.b64encode(bytes(range(160, 192))).decode()
PSK_ONE, PSK_GONE, PSK_TWO, PSK_NEW = (base64.b64encode(bytes([n] * 32)).decode() for n in (1, 2, 3, 4))

A4, A6, B4 = '10.66.66.3/32', 'fd66:66::3/128', '10.66.66.9/32'
# AllowedIPs lines of the last peer (OLD) in each form.
OLD_LINES = {
    'canonical': [f'{A4}, {A6}'],
    'remove-line': [f'{A4}, {B4}, {A6}', f'-{B4}'],
    'plus': [f'+{A4}, +{A6}'],
    'remove-absent': [f'{A4}, {A6}', f'-{B4}'],
    'remove-then-add': [f'-{A4}', f'{A4}, {A6}'],
}
# `wg setconf` of each form on a fresh wg0, then `wg show wg0 allowed-ips` (Linux 7.0, wireguard-tools 1.0.20250521);
# wireguard-tools 1.0.20210914 rejects every form with a signed entry: "Unable to parse IP address: `-10.66.66.9'".
WG_ORACLE = {
    'canonical': {A4, A6},
    'remove-line': {A4, A6},
    'plus': {A4, A6},
    'remove-absent': {A4, A6},
    'remove-then-add': {A4, A6},
}


def conf(form):
    """As 28-wireguard writes the header and backend appends the peers; OLD gets the lines of the form."""
    text = ('# Initial config — panel will overwrite via syncWireguardConfig().\n[Interface]\n'
            f'Address = 10.66.66.1/24, fd66:66::1/64\nListenPort = 51820\nPrivateKey = {SERVER}\n')
    for key, psk, host in ((PEER, PSK_ONE, 2), (GONE, PSK_GONE, 4), (OLD, PSK_TWO, 3)):
        lines = OLD_LINES[form] if key == OLD else [f'10.66.66.{host}/32, fd66:66::{host}/128']
        text += (f'\n[Peer]\nPublicKey = {key}\nPresharedKey = {psk}\n' +
                 ''.join(f'AllowedIPs = {line}\n' for line in lines))
    return text


def wg_view(text):
    """config.c: [interface values, [peer values]]; AllowedIPs entries add, `-` entries remove, in order."""
    interface, peers, section = {}, [], None
    for raw in text.split('\n'):
        line = re.sub(r'[ \t\n\v\f\r]', '', raw.split('#', 1)[0])
        if not line:
            continue
        if line.encode().lower() in (b'[interface]', b'[peer]'):
            section = line.lower()
            if section == '[peer]':
                peers.append({})
            continue
        name, _, value = line.partition('=')
        name = name.encode().lower().decode()
        fields = interface if section == '[interface]' else peers[-1]
        if name != 'allowedips':
            fields[name] = value
            continue
        allowed = fields.setdefault(name, [])
        for entry in filter(None, value.split(',')):
            address = entry[1:] if entry[0] in '+-' else entry
            if entry[0] == '-':
                if address in allowed:
                    allowed.remove(address)
            elif address not in allowed:
                allowed.append(address)
    return [interface, peers]


def wg_sets(text):
    return {p['publickey']: set(p.get('allowedips', [])) for p in wg_view(text)[1]}


def installed_wg(text):
    """The wg installed here on `text` as a node takes it: wg-quick strip, `wg setconf` on a fresh wg0 in a netns.

    (None, {peer: set of AllowedIPs}) if wg applies the file, (the entry wg cannot parse, None) if it rejects it.
    """
    from test_wg_conf_wg_oracle import oracle  # it imports this module
    error, _key, peers = oracle(text.encode())
    if error is None:
        return None, {key: set(ips) for key, _psk, ips in peers}
    found = re.search(r"Unable to parse IP address: `([^']*)'", error)
    assert found, error
    return found.group(1), None


def signed(address):
    """An entry wg rejected for its sign: the edge where wg versions part."""
    return address is not None and address[:1] in ('+', '-')


def assert_rejected_like_wg(case, form, address, backup, gate, refusing):
    """wg rejects the file at `address`: backup, verify and restore_wg_apply refuse it naming that entry.

    backup() -> (addresses, complaints), gate() -> wg_apply_check notes, refusing: {name: call that must raise}.
    """
    named = ('wg0.conf: wg отвергает файл', f'`AllowedIPs`: Unable to parse IP address `{address}`')
    with case.subTest('backup refuses like wg', form=form):
        addresses, bad = backup()
        case.assertEqual(addresses, {})
        case.assertTrue([b for b in bad if all(n in b for n in named)], bad)
    for name, call in refusing.items():
        with case.subTest(f'verify {name} refuses like wg', form=form):
            with case.assertRaises(ValueError) as caught:
                call()
            for part in named:
                case.assertIn(part, str(caught.exception))
    with case.subTest('verify peers vs wg0 refuses like wg', form=form):
        notes = gate()
        case.assertEqual(len(notes), 1, notes)
        case.assertEqual(notes[0][0], VT.FAIL, notes)
        for part in named:
            case.assertIn(part, notes[0][1])


def source(path):
    return (ROOT / path).read_text()


def cut(text, start, end):
    assert start in text, start
    body = text.split(start, 1)[1]
    assert end in body, end
    return start + body.split(end, 1)[0]


def backup_check(form):
    """anysda-backup.sh --check-bundle: (peer addresses it reads, complaints about WireGuard peers)."""
    block = cut(source('infra/lib/anysda-backup.sh'), "wg = root / 'etc-wireguard/wg0.conf'", '\nswan = root')
    rows = [(1, True, (PEER, PSK_ONE, '10.66.66.2')),
            (2, None, (NEW, PSK_NEW, '10.66.66.3')),  # reissued: old key still on its address
            (3, True, (GONE, PSK_GONE, '10.66.66.4'))]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'etc-wireguard').mkdir()
        (root / 'etc-wireguard/wg0.conf').write_text(conf(form))
        scope = {'re': re, 'root': root, 'bad': [], 'retired_keys': set(), 'label': lambda d: f'устройство {d}',
                 'device_columns': {'wg_public_key', 'wg_preshared_key', 'wg_ip'},
                 'devices': lambda *_fields: iter(rows)}
        exec(block, scope)
    return scope['addresses'], scope['bad']


def load_verify():
    spec = importlib.util.spec_from_file_location('verify_traffic_prefix', ROOT / 'infra/lib/verify-traffic.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VT = load_verify()


def verify_conf_peers(form):
    """verify-traffic.py conf_peers(): addresses of each peer as restore_wg_apply and wg.apply take them."""
    return {p['key']: sorted(p['allowed']) for p in VT.conf_peers(conf(form))}


def verify_apply_gate(form):
    """verify-traffic.py wg_apply_check() up to the free address: its verdict when live wg0 holds the conf's peers."""
    notes = []

    class Report:
        @staticmethod
        def add(_label, _component, status, text):
            notes.append((status, text))

    class Done:
        stdout = ' '.join(wg_sets(conf(form)))

    def run(*args, **_kw):
        assert args == ('wg', 'show', 'wg0', 'peers'), args
        return Done

    saved = VT.WG_CONF, VT.CONTROL_SOCKET, VT.CONTROL_WG_STATE, VT.run, VT.WireGuard.free_addresses
    with tempfile.TemporaryDirectory() as directory:
        VT.WG_CONF, VT.CONTROL_SOCKET = Path(directory) / 'wg0.conf', Path(directory)
        VT.CONTROL_WG_STATE = Path(directory) / 'absent.json'
        VT.WG_CONF.write_text(conf(form))
        VT.run = run
        VT.WireGuard.free_addresses = staticmethod(lambda _count: [])
        try:
            VT.wg_apply_check(Report)
        finally:
            VT.WG_CONF, VT.CONTROL_SOCKET, VT.CONTROL_WG_STATE, VT.run = saved[:4]
            VT.WireGuard.free_addresses = staticmethod(saved[4])
    return notes


def verify_syncconf_sets(form):
    """verify-traffic.py restore_wg_apply(): peer addresses wg reads from `wg syncconf wg0 /dev/stdin`."""
    sent = []
    saved = VT.WORK, VT.run, VT.restore_file
    with tempfile.TemporaryDirectory() as directory:
        VT.WORK = Path(directory)
        (VT.WORK / 'wg0.conf').write_text(conf(form))
        VT.run = lambda *args, data=None, **_kw: sent.append((args, data))
        VT.restore_file = lambda *_args: None
        try:
            VT.restore_wg_apply()
        finally:
            VT.WORK, VT.run, VT.restore_file = saved
    [(args, data)] = sent
    assert args[:3] == ('wg', 'syncconf', 'wg0'), args
    return wg_sets(data)


PASSED_GATE = [(VT.UNKNOWN, 'нет свободного адреса в подсети wg0')]
WG_APPLY = [{'key': PEER, 'preshared_key': PSK_ONE, 'ip': '10.66.66.2'},
            {'key': GONE, 'preshared_key': PSK_GONE, 'ip': '10.66.66.4'},
            {'key': OLD, 'preshared_key': PSK_TWO, 'ip': '10.66.66.3'}]


@as_root
class WgConfAllowedIpsPrefixTests(unittest.TestCase):
    """1 `-B` after `A, B`, 2 `+A`, 3 `-X` absent, 4 `-A` then `A`, 5 the file as written."""

    def test_0_reference_matches_wg(self):
        for form, wanted in WG_ORACLE.items():
            with self.subTest(form):
                self.assertEqual(wg_sets(conf(form))[OLD], wanted)
                self.assertEqual(wg_sets(conf(form))[PEER], {'10.66.66.2/32', 'fd66:66::2/128'})
            with self.subTest('installed wg', form=form):
                address, sets = installed_wg(conf(form))
                if signed(address):
                    self.assertIn(address, ''.join(OLD_LINES[form]))
                else:
                    self.assertIsNone(address)
                    self.assertEqual(sets, wg_sets(conf(form)))

    def assert_reads_like_wg(self, form):
        wanted = wg_sets(conf(form))
        address, sets = installed_wg(conf(form))
        if address is not None:
            assert_rejected_like_wg(self, form, address, lambda: backup_check(form), lambda: verify_apply_gate(form),
                                    {'conf_peers': lambda: verify_conf_peers(form),
                                     'apply_peers': lambda: VT.apply_peers(conf(form)),
                                     'syncconf body': lambda: verify_syncconf_sets(form)})
            return
        with self.subTest('installed wg', form=form):
            self.assertEqual(sets, wanted)
        addresses, bad = backup_check(form)
        with self.subTest('backup addresses', form=form):
            self.assertEqual(addresses, wanted)
        with self.subTest('backup complaints', form=form):
            self.assertEqual(bad, [])
        with self.subTest('verify conf_peers', form=form):
            self.assertEqual(verify_conf_peers(form), {key: sorted(value) for key, value in wanted.items()})
        with self.subTest('verify apply_peers', form=form):
            self.assertEqual(VT.apply_peers(conf(form)), WG_APPLY)
        with self.subTest('verify peers vs wg0', form=form):
            self.assertEqual(verify_apply_gate(form), PASSED_GATE)
        with self.subTest('verify syncconf body', form=form):
            self.assertEqual(verify_syncconf_sets(form), wanted)

    def test_1_removed_line_drops_the_address(self):
        self.assert_reads_like_wg('remove-line')

    def test_2_plus_adds_the_address(self):
        self.assert_reads_like_wg('plus')

    def test_3_removing_an_absent_address_changes_nothing(self):
        self.assert_reads_like_wg('remove-absent')

    def test_4_remove_then_add_keeps_the_address(self):
        self.assert_reads_like_wg('remove-then-add')

    def test_5_file_as_written_reads_as_before(self):
        self.assert_reads_like_wg('canonical')


if __name__ == '__main__':
    unittest.main()
