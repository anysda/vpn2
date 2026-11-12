"""wg0.conf outside apply: a wg name with no value, wg-quick names as wg-quick reads them.

wg (config.c) takes a name only with a value after `=`: `PresharedKey =`, `Endpoint=`,
a value that is only a comment - `Line unrecognized`, the whole file rejected.
wg-quick takes its own names out only in a section whose header is `[Interface]`
as a whole (any case) and trims a name only at its ends: after `[ Interface ]`
and in `Post Up = ...` the line stays for wg, which rejects it. The backup check
(anysda-backup.sh --check-bundle) decides like wg after
wg-quick.
"""
import unittest

from test_wg_conf_allowedips_prefix import SERVER
from test_wg_conf_lines import wg_quick_strip, wg_reject, with_lines, written
from test_wg_conf_module import BASE, CANONICAL, backup_check, conf
from wg_root import as_root

WG_NAMES = {'interface': ('PrivateKey', 'ListenPort', 'FwMark'),
            'peer': ('PublicKey', 'PresharedKey', 'AllowedIPs', 'Endpoint', 'PersistentKeepalive')}
# A name with no value, put into [Interface] after PrivateKey or into the last [Peer].
EMPTY = {'eq-spaced': '{} =', 'eq-tight': '{}=', 'eq-trailing-spaces': '{} =   ', 'eq-comment': '{} = # note'}
# (header of the first section, lines after PrivateKey) - wg-quick names around its own rules.
WG_QUICK_FORMS = {
    'section-inner-spaces-address': ('[ Interface ]', 'Address = 10.66.66.1/24'),
    'section-inner-spaces-postup': ('[ Interface ]', 'PostUp = true'),
    'section-lower': ('[interface]', 'Address = 10.66.66.1/24\nPostUp = true'),
    'section-upper': ('[INTERFACE]', 'Address = 10.66.66.1/24\nPostUp = true'),
    'name-inner-space': ('[Interface]', 'Post Up = true'),
    'name-edge-spaces': ('[Interface]', ' Address = 10.66.66.1/24\n\tPostUp = true '),
    'name-tab-before-eq': ('[Interface]', 'Address\t= 10.66.66.1/24\nPostUp\t=\ttrue'),
}
# `wg setconf wg0 /dev/stdin` of the output of `wg-quick strip` of each form, on a fresh wg0 in a netns
# (Linux 7.0, wireguard-tools 1.0.20250521, copies of /usr/bin/wg and /usr/bin/wg-quick):
# the error wg rejects the whole file with, None if it applies.
WG_ORACLE = {
    **{('empty', name, form): f'Line unrecognized: `{name}=\''
       for names in WG_NAMES.values() for name in names for form in EMPTY},
    ('wg-quick', 'section-inner-spaces-address', ''): 'Line unrecognized: `Address=10.66.66.1/24\'',
    ('wg-quick', 'section-inner-spaces-postup', ''): 'Line unrecognized: `PostUp=true\'',
    ('wg-quick', 'section-lower', ''): None,
    ('wg-quick', 'section-upper', ''): None,
    ('wg-quick', 'name-inner-space', ''): 'Line unrecognized: `PostUp=true\'',
    ('wg-quick', 'name-edge-spaces', ''): None,
    ('wg-quick', 'name-tab-before-eq', ''): None,
    ('written', 'written', ''): None,
}


def interface(header, lines):
    """conf(BASE) without Address, the first section header replaced, the lines after PrivateKey."""
    text = conf(BASE, address=False)
    line = f'PrivateKey = {SERVER}\n'
    assert text.count('[Interface]\n') == 1 and text.count(line) == 1, text
    return text.replace('[Interface]\n', header + '\n').replace(line, line + lines + '\n')


def forms():
    """(kind, name, form) -> wg0.conf text of every form above and the file as written."""
    found = {('empty', name, form): with_lines(place, line.format(name))
             for place, names in WG_NAMES.items() for name in names for form, line in EMPTY.items()}
    found.update({('wg-quick', form, ''): interface(*parts) for form, parts in WG_QUICK_FORMS.items()})
    found['written', 'written', ''] = written()
    return found


@as_root
class WgConfLinesMoreTests(unittest.TestCase):
    """1 a wg name with no value, 2 wg-quick names, 3 the module's directory, 0 the model, 5 own files."""

    def test_0_reference_matches_wg(self):
        for key, text in forms().items():
            with self.subTest(key=key):
                self.assertEqual(wg_reject(wg_quick_strip(text)), WG_ORACLE[key])

    def assert_rejected(self, text, **where):
        code, out, err = backup_check(text)
        with self.subTest('backup exit 3', **where):
            self.assertEqual(code, 3, (out, err))
        with self.subTest('backup names wg0.conf', **where):
            self.assertTrue([line for line in out if 'wg0.conf' in line], (out, err))

    def assert_accepted(self, text, **where):
        with self.subTest('backup: no complaint', **where):
            code, out, err = backup_check(text)
            self.assertEqual((code, out), (0, []), err)

    def test_1_wg_name_without_value_is_rejected(self):
        for (kind, name, form), text in forms().items():
            if kind == 'empty':
                self.assert_rejected(text, name=name, form=form)

    def test_2_wg_quick_names_decided_like_wg_quick_then_wg(self):
        for (kind, form, _), text in forms().items():
            if kind != 'wg-quick':
                continue
            if WG_ORACLE[kind, form, '']:
                self.assert_rejected(text, form=form)
            else:
                self.assert_accepted(text, form=form)

    def test_5_own_files_pass(self):
        self.assert_accepted(written(), form='written')
        self.assert_accepted(conf(CANONICAL), form='canonical')
        self.assert_accepted(with_lines('interface', 'Table = off\nSaveConfig = false\nDNS = 1.1.1.1\nMTU = 1420'),
                             form='wg-quick-names')


if __name__ == '__main__':
    unittest.main()
