"""wg0.conf outside apply: an empty AllowedIPs entry, a peer repeated in a second [Peer] section.

wg rejects the whole file when an AllowedIPs list has an empty entry (`X,`,
`, X`, `X,,Y`). Every [Peer] section goes to the kernel as its own peer entry,
in order: a section whose entries carry no sign replaces the peer's addresses,
one with a `+X` or `-X` entry edits them. The backup check, verify-traffic.py
and the `wg syncconf` body of restore_wg_apply must end with the sets wg ends
with, or fail like wg does.
"""
import re
import unittest

from test_wg_conf_allowedips_net import G4, G6, SPLIT, apply_gate, backup_check, network, syncconf_body
from test_wg_conf_allowedips_prefix import (
    A4, A6, B4, GONE, OLD, PASSED_GATE, PEER, PSK_GONE, PSK_ONE, PSK_TWO, SERVER, VT, WG_APPLY)
from wg_root import as_root

# 1 wg rejects the whole file: AllowedIPs lines of OLD with an empty entry.
EMPTY = {
    'comma-trailing': [f'{A4},'],
    'comma-leading': [f',{A4}'],
    'comma-double': [f'{A4},,{B4}'],
    'comma-trailing-spaced': [f'{A4} , '],
    'comma-leading-spaced': [f' , {A4}'],
    'comma-double-spaced': [f'{A4} , , {B4}'],
    'comma-second-line': [A4, f'{A6},'],
}
# 2 OLD in its section, then OLD again in a last section: (lines of the first, lines of the second).
AGAIN = {
    'again-plus': ([A4], [f'+{B4}']),
    'again-plus-v6': ([A4], [f'+{A6}']),
    'again-minus': ([f'{A4}, {A6}'], [f'-{A6}']),
    'again-plain': ([f'{A4}, {B4}'], [f'{A4}, {A6}']),
}
# `wg setconf` of each form (no Address line) on a fresh wg0 in a netns, then `wg show wg0 allowed-ips`
# (Linux 7.0, wireguard-tools 1.0.20250521): {peer: set}. Every EMPTY form: "Unable to parse IP address".
WG_ORACLE = {
    'again-plus': {GONE: {G4, G6}, OLD: {A4, B4}},
    'again-plus-v6': {GONE: {G4, G6}, OLD: {A4, A6}},
    'again-minus': {GONE: {G4, G6}, OLD: {A4}},
    'again-plain': {GONE: {G4, G6}, OLD: {A4, A6}},
}


def conf(form):
    """As 28-wireguard writes the header and backend appends the peers; OLD gets the form's lines."""
    old, again = (EMPTY[form], None) if form in EMPTY else AGAIN[form]
    sections = [(PEER, PSK_ONE, ['10.66.66.2/32, fd66:66::2/128']), (GONE, PSK_GONE, [f'{G4}, {G6}']),
                (OLD, PSK_TWO, old)] + ([(OLD, PSK_TWO, again)] if again is not None else [])
    text = ('# Initial config — panel will overwrite via syncWireguardConfig().\n[Interface]\n'
            f'Address = 10.66.66.1/24, fd66:66::1/64\nListenPort = 51820\nPrivateKey = {SERVER}\n')
    for key, psk, lines in sections:
        text += (f'\n[Peer]\nPublicKey = {key}\nPresharedKey = {psk}\n' +
                 ''.join(f'AllowedIPs = {line}\n' for line in lines))
    return text


def sections(text):
    """config.c: [(peer, [AllowedIPs entries])] per [Peer] section in order; ValueError where wg rejects the file."""
    found, section = [], None
    for raw in text.split('\n'):
        line = re.sub(r'[ \t\n\v\f\r]', '', raw.split('#', 1)[0])
        if not line:
            continue
        if line.encode().lower() in (b'[interface]', b'[peer]'):
            section = line.lower()
            continue
        name, _, value = line.partition('=')
        name = name.encode().lower().decode()
        if section != '[peer]':
            continue
        if name == 'publickey':
            found.append((value, []))
        elif name == 'allowedips':
            if not value:
                raise ValueError(f'Line unrecognized: `{line}`')
            for entry in value.split(','):
                sign = entry[:1] if entry[:1] in ('+', '-') else ''
                found[-1][1].append((sign, network(entry[len(sign):])))
    return found


def wg_apply(text, owners=None):
    """The kernel takes each section as a peer entry: no sign replaces the peer's set, `+X` adds, `-X` drops X.

    owners is the live wg0 ({network: peer}); None is `wg setconf` on a fresh wg0. Peers the text has not
    are dropped, as `wg syncconf` and `wg setconf` do. Returns {peer: set of networks}.
    """
    owners = dict(owners or {})
    found = sections(text)
    keys = {peer for peer, _ in found}
    owners = {net: peer for net, peer in owners.items() if peer in keys}
    for peer, entries in found:
        if not any(sign for sign, _ in entries):
            owners = {net: owner for net, owner in owners.items() if owner != peer}
        for sign, net in entries:
            if sign == '-':
                if owners.get(net) == peer:
                    del owners[net]
            else:
                owners[net] = peer
    sets = {peer: set() for peer in keys}
    for net, peer in owners.items():
        sets[peer].add(net)
    return sets


def live(sets):
    return {net: peer for peer, nets in sets.items() for net in nets}


@as_root
class WgConfAllowedIpsEdgesTests(unittest.TestCase):
    """1 empty entry in the list, 2 the peer repeated in a second section, 0 the model against wg."""

    def test_0_reference_matches_wg(self):
        for form, wanted in WG_ORACLE.items():
            with self.subTest(form):
                sets = wg_apply(conf(form))
                self.assertEqual({GONE: sets[GONE], OLD: sets[OLD]}, wanted)
                self.assertEqual(sets[PEER], {'10.66.66.2/32', 'fd66:66::2/128'})
        for form in EMPTY:
            with self.subTest(form):
                with self.assertRaises(ValueError) as caught:
                    wg_apply(conf(form))
                self.assertIn('Unable to parse IP address', str(caught.exception))

    def test_1_empty_entry_is_not_read_silently(self):
        for form in EMPTY:
            text = conf(form)
            try:
                addresses, bad = backup_check(text)
            except Exception as error:
                with self.subTest('backup fails', form=form):
                    self.assertIn('AllowedIPs', str(error))
            else:
                with self.subTest('backup fails', form=form):
                    self.assertTrue([b for b in bad if 'AllowedIPs' in b], (addresses, bad))
            for name, call in (('conf_peers', VT.conf_peers), ('apply_peers', VT.apply_peers)):
                with self.subTest(f'verify {name} fails', form=form):
                    with self.assertRaises(Exception) as caught:
                        call(text)
                    self.assertIn('AllowedIPs', str(caught.exception))
            with self.subTest('verify peers vs wg0', form=form):
                try:
                    notes = apply_gate(text)
                except Exception as error:
                    self.assertIn('AllowedIPs', str(error))
                else:
                    self.assertTrue(notes and notes != PASSED_GATE and 'AllowedIPs' in notes[0][1], notes)

    def test_2_repeated_peer_reads_like_wg(self):
        for form in AGAIN:
            text, wanted = conf(form), wg_apply(conf(form))
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
            with self.subTest('syncconf body on a fresh wg0', form=form):
                try:
                    self.assertEqual(wg_apply(body), wanted)
                except ValueError as error:
                    self.fail(f'wg rejects the body: {error}')
            with self.subTest('syncconf body on wg0 holding the conf', form=form):
                try:
                    self.assertEqual(wg_apply(body, live(wanted)), wanted)
                except ValueError as error:
                    self.fail(f'wg rejects the body: {error}')


if __name__ == '__main__':
    unittest.main()
