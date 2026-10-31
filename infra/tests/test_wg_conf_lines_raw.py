"""wg0.conf outside apply: a section header is the whole line, a lone `\\r` is part of a line.

wg (config.c) takes a line as a section header only when the whole line without
whitespace is `[Interface]` or `[Peer]` (any case): `[Peer]=x`, `[Interface]=`,
`[ Peer ]=x`, `[Peer]x` - `Line unrecognized`, the whole file rejected. wg reads
lines up to `\\n` only, a lone `\\r` is whitespace inside the line: `[Peer]\\rPublicKey = ...`
is one line wg does not recognize, `PersistentKeepalive\\r= 25` is a good one,
`# old\\rFoo = 1` is a comment. A file with CRLF line ends passes. The backup check
(anysda-backup.sh --check-bundle) and verify-traffic.py read wg0.conf from disk and
decide like wg after wg-quick.
"""
import re
import unittest

from test_wg_conf_allowedips_net import apply_gate, syncconf_body, wg_sets
from test_wg_conf_allowedips_prefix import A4, A6, OLD, SERVER, VT
from test_wg_conf_lines import wanted_peers, wg_quick_strip, wg_reject, written
from test_wg_conf_module import BASE, CANONICAL, backup_check, conf
from wg_root import as_root

# Edge 1: a line starting with a section name, more on the line - put as the first header or after the last peer.
HEADERS = {
    'peer-eq-x': ('tail', '[Peer]=x'),
    'interface-eq-empty': ('header', '[Interface]='),
    'interface-eq-x-address': ('header', '[Interface]=x'),
    'peer-inner-spaces-eq-x': ('tail', '[ Peer ]=x'),
    'peer-spaced-eq-x': ('tail', '[Peer] = x'),
    'peer-glued-x': ('tail', '[Peer]x'),
}
# Edge 2: a lone `\r` inside a line, CRLF of the whole file, `\r` at a line end.
CR_REJECTED = {
    'cr-peer-in-one-line': f'[Peer]\rPublicKey = {OLD}\rAllowedIPs = {A4}, {A6}\n',
    'cr-two-allowedips': None,
}
CR_ACCEPTED = {
    'cr-before-eq': 'PersistentKeepalive\r= 25\n',
    'cr-after-comment': '# old\rFoo = 1\n',
    'crlf-whole-file': None,
    'cr-at-line-end': None,
}
# `wg setconf wg0 /dev/stdin` of the output of `wg-quick strip` of each form, on a fresh wg0 in a netns
# (Linux 7.0, wireguard-tools 1.0.20250521, copies of /usr/bin/wg and /usr/bin/wg-quick):
# the error wg rejects the whole file with, None if it applies. wg-quick strip keeps `\r` as it is.
WG_ORACLE = {
    'peer-eq-x': 'Line unrecognized: `[Peer]=x\'',
    'interface-eq-empty': 'Line unrecognized: `[Interface]=\'',
    'interface-eq-x-address': 'Line unrecognized: `[Interface]=x\'',
    'peer-inner-spaces-eq-x': 'Line unrecognized: `[Peer]=x\'',
    'peer-spaced-eq-x': 'Line unrecognized: `[Peer]=x\'',
    'peer-glued-x': 'Line unrecognized: `[Peer]x\'',
    'cr-peer-in-one-line': f'Line unrecognized: `[Peer]PublicKey={OLD}AllowedIPs={A4},{A6}\'',
    'cr-two-allowedips': f'AllowedIP is not in the correct format: `{A4}AllowedIPs={A6}\'',
    'cr-before-eq': None,
    'cr-after-comment': None,
    'crlf-whole-file': None,
    'cr-at-line-end': None,
    'written': None,
    'canonical': None,
}


def form(name):
    """wg0.conf text of a form above: conf(BASE), its last peer OLD with AllowedIPs `A4, A6`."""
    text = conf(BASE, address=name != 'interface-eq-empty')
    last = f'AllowedIPs = {A4}, {A6}\n'
    assert text.endswith(last) and text.count('[Interface]\n') == 1, text
    if name in HEADERS:
        place, line = HEADERS[name]
        if place == 'header':
            return text.replace('[Interface]\n', line + '\n')
        return text + f'{line}\nPublicKey = {OLD}\n'
    if name == 'cr-two-allowedips':
        return text[:-len(last)] + f'AllowedIPs = {A4}\rAllowedIPs = {A6}\n'
    if name == 'crlf-whole-file':
        return text.replace('\n', '\r\n')
    if name == 'cr-at-line-end':
        return text.replace('[Peer]\n', '[Peer]\r\n').replace(f'PrivateKey = {SERVER}\n', f'PrivateKey = {SERVER} \r\n')
    return text + (CR_REJECTED | CR_ACCEPTED)[name]


def forms():
    """name -> wg0.conf text of every form above, the file as written and the canonical one."""
    found = {name: form(name) for name in (*HEADERS, *CR_REJECTED, *CR_ACCEPTED)}
    found['written'] = written()
    found['canonical'] = conf(CANONICAL)
    return found


def reference(text):
    """wg-quick strip, then wg: `Line unrecognized`, an AllowedIPs entry wg does not parse, or None."""
    stripped = wg_quick_strip(text)
    error = wg_reject(stripped)
    if error:
        return error
    try:
        wg_sets(stripped)
    except ValueError:
        return 'AllowedIP is not in the correct format'
    return None


def restored(text):
    """verify-traffic.py restore_wg_apply() of wg0.conf on disk, as wg reads the body: interface lines, peers."""
    body = syncconf_body(text)
    interface = body.split('\n[Peer]', 1)[0].split('\n')
    return sorted(filter(None, (re.sub(r'[ \t\n\v\f\r]', '', line) for line in interface))), wanted_peers(body)


@as_root
class WgConfLinesRawTests(unittest.TestCase):
    """1 a section header with more on the line, 2 a lone `\\r`, 0 the model, 4 own files."""

    def test_0_reference_matches_wg(self):
        for name, text in forms().items():
            with self.subTest(form=name):
                found, wanted = reference(text), WG_ORACLE[name]
                self.assertEqual(found, wanted if found is None or found.startswith('Line') else wanted[:len(found)])

    def assert_rejected(self, text, **where):
        code, out, err = backup_check(text)
        with self.subTest('backup exit 3', **where):
            self.assertEqual(code, 3, (out, err))
        with self.subTest('backup names wg0.conf', **where):
            self.assertTrue([line for line in out if 'wg0.conf' in line], (out, err))
        for name, call in (('conf_peers', VT.conf_peers), ('apply_peers', VT.apply_peers)):
            with self.subTest(f'verify {name} fails', **where):
                with self.assertRaises(ValueError) as caught:
                    call(text)
                self.assertIn('wg0.conf', str(caught.exception))
        with self.subTest('verify reads wg0.conf: FAIL', **where):
            notes = apply_gate(text)
            self.assertTrue(notes and notes[0][0] == VT.FAIL and 'wg0.conf' in notes[0][1], notes)
        with self.subTest('verify restore reads wg0.conf: ValueError', **where):
            with self.assertRaises(ValueError):
                syncconf_body(text)

    def assert_accepted(self, text, **where):
        canonical = conf(CANONICAL)
        with self.subTest('backup: no complaint', **where):
            code, out, err = backup_check(text)
            self.assertEqual((code, out), (0, []), err)
        with self.subTest('verify: the same peers', **where):
            self.assertEqual(wanted_peers(text), wanted_peers(canonical))
            self.assertIsNotNone(VT.apply_peers(text))
        with self.subTest('verify reads wg0.conf: the same as the canonical file', **where):
            self.assertEqual(apply_gate(text), apply_gate(canonical))
        with self.subTest('verify restore reads wg0.conf: the same peers', **where):
            self.assertEqual(restored(text), restored(canonical))

    def test_1_header_is_the_whole_line(self):
        for name in HEADERS:
            self.assertIsNotNone(WG_ORACLE[name])
            self.assert_rejected(form(name), form=name)

    def test_2a_lone_cr_wg_rejects(self):
        for name in CR_REJECTED:
            self.assertIsNotNone(WG_ORACLE[name])
            self.assert_rejected(form(name), form=name)

    def test_2b_lone_cr_wg_accepts(self):
        for name in CR_ACCEPTED:
            self.assertIsNone(WG_ORACLE[name])
            self.assert_accepted(form(name), form=name)

    def test_4_own_files_pass(self):
        self.assert_accepted(written(), form='written')
        self.assert_accepted(conf(CANONICAL), form='canonical')


if __name__ == '__main__':
    unittest.main()
