"""wg0.conf outside apply: the backup check decides like wg itself on every form, keys stay the node's.

The oracle is wg: a copy of wg-quick strips the file, the real `wg setconf` applies the rest to a
fresh wg0 in a netns, `wg show wg0 dump` gives the peers. `vpn2 backup --check-bundle` must end with
the same decision and peers; a file wg rejects is a complaint naming wg0.conf. Root via `sudo -n`.
"""
import base64
import calendar
import io
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_control import module
from vpn2 import entryvpn, pki
from vpn2.backup import check
from wg_oracle import IP, WG, WG_QUICK, as_root, ns_wg, oracle

ROOT = Path(__file__).resolve().parents[3]
S = base64.b64encode(b'0123456789abcdef0123456789abcdef')
A = base64.b64encode(bytes(range(64, 96)))
B = base64.b64encode(bytes(range(96, 128)))
C = base64.b64encode(bytes(range(160, 192)))
STAMP = '2026-10-30T120000Z'
CHECK = [sys.executable, '-B', '-m', 'vpn2.cli', 'backup', '--check-bundle']


def mk(iface=b'', peer=b'', tail=b'', head=b'', header=b'[Interface]', section=b'[Peer]', eol=b'\n'):
    """[Interface] with the lines `iface`, peer A (PSK) with the lines `peer`, peer B (no PSK), then `tail`."""
    text = (head + header + b'\nListenPort = 51820\nPrivateKey = ' + S + b'\n' + iface +
            b'\n' + section + b'\nPublicKey = ' + A + b'\nPresharedKey = ' + S + b'\nAllowedIPs = 10.66.66.3/32\n' +
            peer + b'\n' + section + b'\nPublicKey = ' + B + b'\nAllowedIPs = 10.66.66.4/32\n' + tail)
    return text.replace(b'\n', eol)


def ips(a=b'AllowedIPs = 10.66.66.3/32, fd66:66::3/128\n', b=b'AllowedIPs = 10.66.66.4/32\n', tail=b''):
    """Two peers with PSK; the AllowedIPs lines of A and B, then `tail`."""
    return (b'[Interface]\nListenPort = 51820\nPrivateKey = ' + S + b'\n\n[Peer]\nPublicKey = ' + A +
            b'\nPresharedKey = ' + S + b'\n' + a + b'\n[Peer]\nPublicKey = ' + B + b'\nPresharedKey = ' + S +
            b'\n' + b + tail)


def allowed(*lines):
    return b''.join(b'AllowedIPs = ' + line + b'\n' for line in lines)


def put(place, lines):
    """Lines after PrivateKey of [Interface] or at the end of the last [Peer]."""
    return mk(iface=lines + b'\n') if place == 'iface' else mk(tail=lines + b'\n')


def renamed(pairs):
    text = mk(iface=b'FwMark = 0x66\n')
    for old, new in pairs:
        text = text.replace(old.encode(), new.encode())
    return text


NAMES = {'iface': ('PrivateKey', 'ListenPort', 'FwMark'),
         'peer': ('PublicKey', 'PresharedKey', 'AllowedIPs', 'Endpoint', 'PersistentKeepalive')}
WG_QUICK_NAMES = b'Address = 10.66.66.1/24\nDNS = 1.1.1.1\nMTU = 1420\nTable = off\nPreUp = true\nPostUp = true\n' \
                 b'PreDown = true\nPostDown = true\nSaveConfig = false'
# Values wg rejects.
VALUES = {
    'pubkey-bad-format': mk(tail=b'[Peer]\nPublicKey = abc\nAllowedIPs = 10.66.66.9/32\n'),
    'listenport-abc': mk(iface=b'ListenPort = abc\n'),
    'keepalive-abc': mk(peer=b'PersistentKeepalive = abc\n'),
    'endpoint-bad': mk(peer=b'Endpoint = ::::\n'),
    'psk-bad': mk(peer=b'PresharedKey = abc\n'),
    'eq-eq-value': mk(peer=b'PersistentKeepalive == 25\n'),
    'pubkey-abc-eq': mk(tail=b'[Peer]\nPublicKey = abc=\n'),
    'psk-eq-mid': mk(peer=b'PresharedKey = ab=cd\n'),
}
# Bytes that are not UTF-8, BOM, NUL (wg-quick drops NUL before wg).
BYTES = {
    'latin1-comment': mk(peer=b'PersistentKeepalive = 25 # caf\xe9\n'),
    'latin1-in-name': mk(peer=b'Persistent\xe9Keepalive = 25\n'),
    'bom': mk(head=b'\xef\xbb\xbf'),
    'latin1-postup': mk(iface=b'PostUp = echo caf\xe9\n'),
    'latin1-in-header': mk(tail=b'[Peer\xe9]\nPublicKey = ' + B + b'\n'),
    'bom-mid': mk(peer=b'\xef\xbb\xbfPersistentKeepalive = 25\n'),
    'nul-in-name': mk(peer=b'Persistent\x00Keepalive = 25\n'),
    'nul-in-wgquick-name': mk(iface=b'Add\x00ress = 10.66.66.1/24\n'),
    'nul-after-value': mk(peer=b'PersistentKeepalive = 25\x00Foo\n'),
    'nul-in-header': mk(tail=b'[Peer]\x00x\nPublicKey = ' + A + b'\nAllowedIPs = 10.66.66.9/32\n'),
    'nul-peer-header': mk(tail=b'[Pe\x00er]\nPublicKey = ' + A + b'\nAllowedIPs = 10.66.66.9/32\n'),
}
# Line ends, blanks, case, comments, sections, wg-quick names.
LINES = {
    'plain': mk(),
    'keepalive-cr-eq': mk(peer=b'PersistentKeepalive\r= 25\n'),
    'foo-cr': mk(peer=b'# old\rFoo = 1\n'),
    'privkey-after-cr': mk(iface=b'# old\rPrivateKey = ' + C + b'\n'),
    'pubkey-after-cr': mk(peer=b'# old\rPublicKey = ' + C + b'\n'),
    'crlf': mk(peer=b'PersistentKeepalive = 25\n', eol=b'\r\n'),
    'crlf-whole': mk(iface=b'Address = 10.66.66.1/24\nPostUp = true\n', eol=b'\r\n'),
    'iface-cr-only': mk(header=b'[Interface]\r', iface=b'Address = 10.66.66.1/24\n'),
    'cr-only-eol': mk(peer=b'PersistentKeepalive = 25\r'),
    'cr-at-line-end': mk().replace(b'[Peer]\n', b'[Peer]\r\n').replace(S + b'\n', S + b' \r\n', 1),
    'peer-cr-pubkey': mk(tail=b'[Peer]\rPublicKey = ' + B + b'\rAllowedIPs = 10.66.66.9/32\n'),
    'allowed-cr-allowed': mk(peer=b'AllowedIPs = 10.66.66.7/32\rAllowedIPs = 10.66.66.8/32\n'),
    'emsp-before-address': mk(iface=' Address = 10.66.66.1/24\n'.encode()),
    'saveconfig-noeq': mk(iface=b'SaveConfig\n'),
    'lower-pubkey': mk(peer=b'publickey = ' + A + b'\n'),
    'upper-allowed': mk(peer=b'ALLOWEDIPS = 10.66.66.9/32\n'),
    'tab-around-eq': mk(peer=b'\tPresharedKey\t=\t' + S + b'\t\n'),
    'space-in-name': mk(peer=b'Persistent Keepalive = 25\n'),
    'space-in-section': mk(section=b'[ Peer ]'),
    'lower-section': mk(section=b'[peer]'),
    'only-eq': mk(peer=b'=\n'),
    'comment-after': mk(peer=b'PersistentKeepalive = 25 # keep\n'),
    'address-in-peer': mk(peer=b'Address = 10.66.66.3/32\n'),
    'postup-in-peer': mk(peer=b'PostUp = true\n'),
    'iface-repeat': mk(peer=b'[Interface]\nListenPort = 51821\n'),
    'iface-wgquick': mk(iface=WG_QUICK_NAMES + b'\n'),
    'iface-space-section-addr': mk(header=b'[ Interface ]', iface=b'Address = 10.66.66.1/24\n'),
    'iface-space-section-postup': mk(header=b'[ Interface ]', iface=b'PostUp = true\n'),
    'space-in-wgquick-name': mk(iface=b'Post Up = true\n'),
    'tab-in-wgquick-name': mk(iface=b'Post\tUp = true\n'),
    'wgquick-name-edge-spaces': mk(iface=b' Address = 10.66.66.1/24\n\tPostUp = true \n'),
    'wgquick-tab-before-eq': mk(iface=b'Address\t= 10.66.66.1/24\nPostUp\t=\ttrue\n'),
    'address-noeq': mk(iface=b'Address\n'),
    'postup-empty': mk(iface=b'PostUp =\n'),
    'crlf-lower-section': mk(section=b'[pEeR]', eol=b'\r\n'),
    'name-only-key-noeq': mk(peer=b'PresharedKey ' + S + b'\n'),
    'typo-privkey-noeq': mk(iface=b'PrivatKey' + S + b'\n'),
    'privkey-in-peer': mk(peer=b'PrivateKey = ' + S + b'\n'),
    'vtab-around': mk(peer=b'PersistentKeepalive\x0b=\x0c25\n'),
    'address-empty': mk(iface=b'Address =\n'),
    'nbsp-in-name': mk(peer=b'Persistent\xc2\xa0Keepalive = 25\n'),
    'iface-after-peer-wgquick': mk(peer=b'[Interface]\nAddress = 10.66.66.1/24\n'),
    'mtu-in-peer': mk(peer=b'mtu = 1420\n'),
    'saveconfig-in-peer': mk(peer=b'SaveConfig = false\n'),
    'dns-in-peer': mk(peer=b'DNS = 1.1.1.1\n'),
    'table-in-peer': mk(peer=b'Table = off\n'),
    'iface-sp-after': mk(header=b'[Interface] ', iface=b'Address = 10.66.66.1/24\n'),
    'iface-tab-after': mk(header=b'[Interface]\t', iface=b'Address = 10.66.66.1/24\n'),
    'iface-comment': mk(header='[Interface] # комм'.encode(), section=b'[Peer] # dev'),
    'iface-repeat-addr': mk(head=b'[Interface]\n', iface=b'Address = 10.66.66.1/24\n'),
    'iface-repeat-after-peer': mk(tail=b'[Interface]\nAddress = 10.66.66.1/24\nMTU = 1420\n'),
    'iface-lower': mk(header=b'[interface]', iface=b'Address = 10.66.66.1/24\nPostUp = true\n'),
    'iface-upper': mk(header=b'[INTERFACE]', iface=b'Address = 10.66.66.1/24\n'),
    'iface-vt-before': mk(header=b'\x0b[Interface]', iface=b'Address = 10.66.66.1/24\n'),
    'iface-vt-after': mk(header=b'[Interface]\x0b', iface=b'Address = 10.66.66.1/24\n'),
    'iface-nbsp-after': mk(header=b'[Interface]\xc2\xa0', iface=b'Address = 10.66.66.1/24\n'),
    'iface-hash-noeq-addr': mk(header=b'[Interface]#', iface=b'Address = 10.66.66.1/24\n'),
    'addr-vt-name': mk(iface=b'Address\x0b= 10.66.66.1/24\n'),
    'addr-cr-name': mk(iface=b'Address\r= 10.66.66.1/24\n'),
    'addr-lead-sp': mk(iface=b'  \tAddress = 10.66.66.1/24\n'),
    'addr-hash-empty': mk(iface=b'Address =#x\n'),
    'address-space': mk(iface=b'Address = 10.66.66.1/24 fd66:66::1/64\n'),
    'address-two-lines': mk(iface=b'Address = 10.66.66.1/24\nAddress = fd66:66::1/64\n'),
    'allowed-two-lines': mk(peer=b'AllowedIPs = fd66:66::3/128\n'),
    'wgquick-before-section': mk(head=b'Address = 10.66.66.1/24\n'),
    'mtu-noeq': mk(iface=b'MTU\n'),
    'eq-name-empty': mk(iface=b'= 5\n'),
    'value-comment': mk(eol=b' # note\n'),
    'blanks': mk(header=b'  [Interface]\t', section=b'\t[Peer]  ', eol=b'#x\n'),
    'commented-out': mk(iface=b'# [Peer]\n#PrivateKey = ' + C + b'\n',
                        tail=b'\n# [Peer]\n#PublicKey = ' + C + b'\n#AllowedIPs = 10.66.66.9/32\n'),
    'blank-in-name': mk().replace(b'PrivateKey', b'Private\tKey').replace(b'PublicKey', b'Public\tKey'),
    'names-mixed-case': renamed([('[Interface]', '[interface]'), ('ListenPort', 'listenport'), ('FwMark', 'FWMARK'),
                                 ('PrivateKey', 'privatekey'), ('PublicKey', 'publickey'), ('[Peer]', '[PEER]'),
                                 ('PresharedKey', 'presharedkey'), ('AllowedIPs', 'allowedips')]),
    # One letter wg does not take for the ASCII one: U+0130, U+017F, U+212A.
    'names-lookalike': renamed([('[Interface]', '[İnterface]'), ('ListenPort', 'LiſtenPort'),
                                ('FwMark', 'FwMarK'), ('PrivateKey', 'PrivateKey'),
                                ('PublicKey', 'PublicKey'), ('AllowedIPs', 'AllowedIPſ')]),
    **{f'{place}-{form}': put(place, line) for place, forms in {
        'iface': {'unknown-key': b'Foo = 1', 'typo': b'ListenPrt = 51821', 'no-equals': b'garbage',
                  'unknown-section': b'[Interfaces]\nListenPort = 51821', 'non-ascii-name': 'PrivateKеy = '.encode() + S,
                  'comments': b'# note\n   # indented note\n\n \t \nFwMark = 0 # mark', 'wg-quick-names': WG_QUICK_NAMES},
        'peer': {'unknown-key': b'Foo = 1', 'typo': b'AllowedIP = 10.66.66.9/32', 'no-equals': b'garbage',
                 'unknown-section': b'[Peers]\nPublicKey = ' + C + b'\nAllowedIPs = 10.66.66.9/32',
                 'non-ascii-name': 'PublicKеy = '.encode() + C, 'comments': b'# note\n\n\t\nPresharedKey = ' + S + b' # psk',
                 'peer-eq-x': b'[Peer]=x\nPublicKey = ' + C, 'peer-spaced-eq-x': b'[Peer] = x\nPublicKey = ' + C,
                 'peer-inner-spaces-eq-x': b'[ Peer ]=x\nPublicKey = ' + C, 'peer-glued-x': b'[Peer]x\nPublicKey = ' + C,
                 'cr-peer-in-one-line': b'[Peer]\rPublicKey = ' + C + b'\rAllowedIPs = 10.66.66.9/32'},
    }.items() for form, line in forms.items()},
    **{f'empty-{name}-{form}': put(place, line.format(name).encode())
       for place, names in NAMES.items() for name in names
       for form, line in {'spaced': '{} =', 'tight': '{}=', 'trailing': '{} =   ', 'comment': '{} = # note',
                          'tab': '{} =\t', 'cr': '{} =\r', 'vt': '{} =\x0b', 'ff': '{} =\x0c', 'hash': '{} =#x'}.items()},
    'iface-eq-empty': mk(header=b'[Interface]='),
    'iface-eq-x-addr': mk(header=b'[Interface]=x', iface=b'Address = 10.66.66.1/24\n'),
}
# A [Peer] without a public key: wg rejects the whole file, the check names PublicKey.
NO_KEY = {
    'pubkey-blank': mk(tail=b'[Peer]\nPublicKey =\nAllowedIPs = 10.66.66.9/32\n'),
    'pubkey-missing': mk(tail=b'[Peer]\nAllowedIPs = 10.66.66.9/32\n'),
    'pubkey-blank-mid': mk(peer=b'\n[Peer]\nPublicKey =\nAllowedIPs = 10.66.66.9/32\n'),
    'pubkey-missing-mid': mk(peer=b'\n[Peer]\nPresharedKey = ' + S + b'\nAllowedIPs = 10.66.66.9/32\n'),
}
# AllowedIPs: `+`/`-` entries, forms of one address, masks, defaults, peers sharing one address.
ALLOWED = {
    'comma-mixed': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32, -10.66.66.9/32, +fd66:66::3/128')),
    'hash-after': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32', b'-10.66.66.9/32 # drop')),
    'hash-before': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32 # , -10.66.66.9/32')),
    'minus-absent': ips(a=allowed(b'10.66.66.3/32', b'-10.66.66.9/32')),
    'minus-after': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32', b'-10.66.66.9/32')),
    'minus-before': ips(a=allowed(b'-10.66.66.3/32', b'10.66.66.3/32')),
    'plus': ips(a=allowed(b'+10.66.66.3/32, +fd66:66::3/128')),
    'sign-then-hash': ips(a=allowed(b'10.66.66.3/32, -#10.66.66.3/32')),
    'space-sign': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32', b'- 10.66.66.9/32')),
    'v6-minus': ips(a=allowed(b'10.66.66.3/32, fd66:66::3/128', b'-fd66:66::3/128')),
    'leading-zero': ips(a=allowed(b'10.66.66.3/32', b'-10.66.66.03/32')),
    'mask-zero': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32', b'-10.66.66.9/032')),
    'nomask-add': ips(a=allowed(b'10.66.66.3')),
    'nomask-minus': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32', b'-10.66.66.9')),
    'only-removed': ips(a=allowed(b'10.66.66.3/32', b'-10.66.66.3')),
    'v6-form-dup': ips(a=allowed(b'10.66.66.3/32, fd66:66::3/128, fd66:66:0::3/128')),
    'v6-form': ips(a=allowed(b'10.66.66.3/32, fd66:66::3/128', b'-fd66:66:0::3/128')),
    'no-allowedips': ips(a=b''),
    'default-both': ips(a=allowed(b'0.0.0.0/0, ::/0'), b=allowed(b'0.0.0.0/0')),
    'default-routes': ips(a=allowed(b'0.0.0.0/0, ::/0')),
    'hostbits-minus': ips(a=allowed(b'10.66.66.0/24', b'-10.66.66.3/24')),
    'hostbits-v6': ips(a=allowed(b'fd66:66::3/64')),
    'hostbits': ips(a=allowed(b'10.66.66.3/24')),
    'minus-other-ab': ips(a=allowed(b'10.66.66.3/32, -10.66.66.4/32')),
    'minus-other': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32'), b=allowed(b'10.66.66.4/32, -10.66.66.9/32')),
    'two-then-minus': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32'),
                          b=allowed(b'10.66.66.4/32, 10.66.66.9/32', b'-10.66.66.9/32')),
    'v6-minus-form': ips(a=allowed(b'10.66.66.3/32, fd66:66::9/128'), b=allowed(b'10.66.66.4/32, -FD66:66:0::9/128')),
    'v6-two-forms': ips(a=allowed(b'10.66.66.3/32, fd66:66::9/128'), b=allowed(b'10.66.66.4/32, fd66:66:0:0::9')),
    'two-peers': ips(a=allowed(b'10.66.66.3/32, 10.66.66.4/32')),
    'double-comma': ips(a=allowed(b'10.66.66.3/32,,10.66.66.9/32')),
    'double-sign': ips(a=allowed(b'10.66.66.3/32, +-10.66.66.9/32')),
    'empty-first': ips(a=b'AllowedIPs =\n' + allowed(b'10.66.66.3/32')),
    'empty-line': ips(a=allowed(b'10.66.66.3/32') + b'AllowedIPs =\n'),
    'empty-only': ips(a=b'AllowedIPs =\n'),
    'leading-comma': ips(a=allowed(b',10.66.66.3/32')),
    'mask-33': ips(a=allowed(b'10.66.66.3/33')),
    'mask-empty': ips(a=allowed(b'10.66.66.3/')),
    'mask-long0': ips(a=allowed(b'10.66.66.3/0000000032')),
    'mask-unicode': ips(a=allowed('10.66.66.3/٣٢'.encode())),
    'mask-v6-129': ips(a=allowed(b'fd66::3/129')),
    'sign-only': ips(a=allowed(b'10.66.66.3/32, -')),
    'trailing-comma': ips(a=allowed(b'10.66.66.3/32,')),
    'unicode-digit': ips(a=allowed('10.66.66.٣/32'.encode())),
    'v4-hex': ips(a=allowed(b'0x0a.66.66.3/32')),
    'v4-short': ips(a=allowed(b'10.66.3/32')),
    'v4mapped': ips(a=allowed(b'::ffff:10.66.66.3/128')),
    'v6-v4tail': ips(a=allowed(b'fd66::10.66.66.3/128')),
    'v6-zone': ips(a=allowed(b'fe80::1%eth0/128')),
    'dup-peer-plus': ips(a=allowed(b'10.66.66.3/32'), tail=b'\n[Peer]\nPublicKey = ' + A + b'\nPresharedKey = ' +
                         S + b'\nAllowedIPs = +10.66.66.9/32\n'),
    'dup-peer': ips(a=allowed(b'10.66.66.3/32'), tail=b'\n[Peer]\nPublicKey = ' + A + b'\nPresharedKey = ' +
                    S + b'\nAllowedIPs = 10.66.66.9/32\n'),
    'canonical': ips(),
    'emptied': ips(a=allowed(b'10.66.66.3/32', b'-10.66.66.3/32')),
}
FORMS = VALUES | BYTES | LINES | NO_KEY | ALLOWED


def backup(raw, rows):
    """--check-bundle on wg0.conf `raw`; rows: (key, PSK, wg_ip, transitional) -> (code, lines)."""
    with tempfile.TemporaryDirectory() as directory:
        root = bundle(directory, raw, rows)
        out = io.StringIO()
        code = check.check_bundle(str(root), out=out, err=out)
    return code, out.getvalue().splitlines()


def bundle(directory, raw, rows):
    root = Path(directory)
    (root / 'manifest.json').write_text(json.dumps({'timestamp': STAMP}))
    now = calendar.timegm(time.strptime(STAMP, '%Y-%m-%dT%H%M%SZ'))
    con = sqlite3.connect(root / 'db.sqlite')
    con.execute('CREATE TABLE clients (id INTEGER PRIMARY KEY, frozen_manual INTEGER, expires_at INTEGER)')
    con.execute('CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, client_id INTEGER, '
                'wg_public_key TEXT, wg_preshared_key TEXT, wg_ip TEXT)')
    for number, (key, psk, ip, transitional) in enumerate(rows, 1):
        con.execute('INSERT INTO clients VALUES (?, 0, ?)', (number, now if transitional else None))
        con.execute('INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?)', (number, f'd{number}', number, key, psk, ip))
    con.commit()
    con.close()
    (root / 'etc-wireguard').mkdir()
    (root / 'etc-wireguard/wg0.conf').write_bytes(raw)
    return root


ONE = [(A.decode(), S.decode(), '10.66.66.3', False)]


def written():
    """wg0.conf as the node has it: the header of wg-identity, the peers backend wg.apply writes."""
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / 'server.priv').write_text(S.decode() + '\n')
        entryvpn.wg_identity('51820', '10.66.66.1', root=directory, run=fake_wg)
        header = (Path(directory) / 'wg0.conf').read_text()
    module('contract')
    peers = [{'key': key.decode(), 'preshared_key': S.decode(), 'ip': ip} for key, ip in ((A, '10.66.66.3'),
                                                                                      (B, '10.66.66.4'))]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'state').mkdir()
        (root / 'etc/wireguard').mkdir(parents=True)
        (root / 'etc/wireguard/wg0.conf').write_text(header)
        backend = module('backend').Backend(root, root / 'state')
        backend.pools = json.loads((ROOT / 'app/shared/vpn-pools.json').read_text())

        def command(*args, data=None):
            del data
            if args[-1] == 'preshared-keys':
                return ''.join(f'{p["key"]}\t{p["preshared_key"]}\n' for p in peers)
            if args[-1] == 'allowed-ips':
                return ''.join(f'{p["key"]}\t{p["ip"]}/32 fd66:66::{p["ip"].split(".")[-1]}/128\n' for p in peers)
            return ''

        with patch.object(backend, 'command', side_effect=command):
            backend.wireguard({'version': 1, 'peers': peers})
        return (root / 'etc/wireguard/wg0.conf').read_bytes()


def fake_wg(argv, *, data=None, timeout=None):
    """wg genkey gives a key the host never had, pubkey of a key gives C."""
    del timeout
    key = C if argv[1:] == ['genkey'] or (data or b'').strip() else b''
    return subprocess.CompletedProcess(argv, 0 if key else 1, key + b'\n', b'')


def identity(raw):
    """wg-identity on a host that has only wg0.conf `raw`: (refusal or None, server.priv)."""
    with tempfile.TemporaryDirectory() as directory:
        top = Path(directory)
        (top / 'wg0.conf').write_bytes(raw)
        try:
            entryvpn.wg_identity('51820', '10.66.66.1', root=top, run=fake_wg)
        except pki.PkiError as error:
            return str(error), None
        return None, (top / 'keys/server.priv').read_text().strip()


class WgIdentityKeepsKeyTests(unittest.TestCase):
    """wg-identity reads the server key of wg0.conf like wg: comments, blanks and case, never a lookalike."""

    def test_server_key_is_the_one_wg_reads(self):
        for name in ('plain', 'value-comment', 'blanks', 'commented-out', 'names-mixed-case', 'iface-comment',
                     'nul-in-name'):
            with self.subTest(name):
                self.assertEqual(identity(FORMS[name]), (None, S.decode()))

    def test_lookalike_name_is_a_refusal_not_a_new_key(self):
        error, key = identity(FORMS['names-lookalike'])
        self.assertIn('cannot replace an active WireGuard private key', error)
        self.assertIsNone(key)


@as_root
@unittest.skipUnless(WG and WG_QUICK and IP, 'needs wg, wg-quick and ip')
class WgOracleTests(unittest.TestCase):
    """Every form: the decision and the peers of the backup check are wg's."""

    def check(self, name, raw):
        error, _private, peers = oracle(raw)
        if error:
            code, lines = backup(raw, ONE)
            with self.subTest('exit 3 naming wg0.conf, like wg rejects', form=name, wg=error):
                self.assertEqual(code, 3, lines)
                self.assertTrue([line for line in lines if 'wg0.conf' in line], lines)
            return
        # Every peer known only by the address it holds in wg: a peer without it is a complaint.
        rows = [(base64.b64encode(bytes([200 + number] * 32)).decode(), 'x', v4[0][:-3], True) if v4 else
                (key, psk or 'x', '', True)
                for number, (key, psk, nets) in enumerate(peers)
                for v4 in [[net for net in nets if re.fullmatch(r'[0-9.]+/32', net)]]]
        with self.subTest('the peers and their addresses are wg\'s', form=name):
            self.assertEqual(backup(raw, rows), (0, []))
        # Every peer an active device with its PSK; a peer without PSK is a device without one.
        rows = [(key, psk, f'10.99.0.{number + 1}', False) for number, (key, psk, _) in enumerate(peers)]
        code, lines = backup(raw, rows)
        disabled = [f'«d{number}»' for number, (_, psk, _) in enumerate(peers, 1) if not psk]
        with self.subTest('the keys and PSKs are wg\'s', form=name):
            self.assertEqual((code, len(lines)), (3 if disabled else 0, len(disabled)), lines)
            for label in disabled:
                self.assertTrue([line for line in lines if label in line], (label, lines))

    def test_oracle(self):
        error, private, peers = oracle(mk())
        self.assertEqual((error, private, [key for key, _, _ in peers]),
                         (None, S.decode(), sorted([A.decode(), B.decode()])))
        self.assertIn('abc', oracle(VALUES['pubkey-bad-format'])[0])
        self.assertIsNone(oracle(BYTES['nul-in-name'])[0])
        self.assertIsNotNone(ns_wg(BYTES['nul-in-name'])[0])

    def test_every_form(self):
        for name, raw in FORMS.items():
            self.check(name, raw)
        for name in VALUES:
            self.assertIsNotNone(oracle(VALUES[name])[0], name)

    def test_peer_without_key_is_named(self):
        for name, raw in NO_KEY.items():
            with self.subTest(name):
                code, lines = backup(raw, ONE)
                self.assertEqual(code, 3, lines)
                self.assertTrue([line for line in lines if 'PublicKey' in line], lines)
                self.assertFalse([line for line in lines if S.decode() in line or A.decode() in line], lines)

    def test_file_as_written_passes(self):
        raw = written()
        self.assertIsNone(oracle(raw)[0])
        self.check('written', raw)

    def test_nothing_to_ask_is_a_loud_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind, path in without_wg(directory).items():
                with patch.dict(os.environ, {'PATH': path}):
                    code, lines = backup(mk(), ONE + [(B.decode(), '', '10.66.66.4', False)])
                with self.subTest(kind=kind):
                    self.assertNotEqual(code, 0, lines)
                    self.assertTrue([line for line in lines if 'wg0.conf' in line], lines)

    def test_endpoint_name_is_a_refusal_in_time(self):
        start = time.monotonic()
        code, lines = backup(mk(peer=b'Endpoint = vpn.example.test:51820\n'), ONE)
        self.assertNotEqual(code, 0, lines)
        self.assertTrue([line for line in lines if 'wg0.conf' in line], lines)
        self.assertLess(time.monotonic() - start, 45)

    def test_kill_mid_check_leaves_no_key(self):
        """Killed after `wg setconf`: no netns, named or held by a process, keeps wg0 with the key."""
        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory)
            mark = top / 'setconf-done'
            (top / 'bin').mkdir()
            (top / 'bin/wg').write_text(f'#!/bin/bash\nif [[ $1 == setconf ]]; then\n  {WG} "$@" || exit\n'
                                        f'  : > {mark}\n  exec sleep 60\nfi\nexec {WG} "$@"\n')
            (top / 'bin/wg').chmod(0o755)
            (top / 'bundle').mkdir()
            root = bundle(top / 'bundle', mk(), ONE)
            env = os.environ | {'PATH': f'{top}/bin{os.pathsep}{os.environ["PATH"]}', 'PYTHONDONTWRITEBYTECODE': '1'}
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with self.subTest(sig.name):
                    mark.unlink(missing_ok=True)
                    child = subprocess.Popen([*CHECK, str(root)], env=env, start_new_session=True,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    deadline = time.monotonic() + 20
                    while not mark.exists() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(mark.exists(), 'the check did not reach the window after setconf')
                    os.killpg(child.pid, sig)
                    child.wait(timeout=30)
                    time.sleep(2)
                    self.assertEqual(holding_key(), [])


def holding_key():
    """Every netns, named or held by a process, with an interface whose private key is S."""
    found, seen = [], {os.stat('/proc/self/ns/net').st_ino}
    named = Path('/run/netns')
    places = [*(named.iterdir() if named.is_dir() else ()), *(p / 'ns/net' for p in Path('/proc').iterdir()
                                                              if p.name.isdigit())]
    for place in places:
        try:
            inode = os.stat(place).st_ino
        except OSError:
            continue
        if inode not in seen:
            seen.add(inode)
            done = subprocess.run(['nsenter', f'--net={place}', WG, 'show', 'all', 'private-key'],
                                  capture_output=True, text=True, timeout=30, check=False)
            if S.decode() in done.stdout:
                found.append(str(place))
    return found


def without_wg(directory):
    """PATH dirs as now: one without wg and wg-quick, one refusing a new netns (`ip netns add`, `unshare --net`)."""
    found = {}
    for folder in os.environ['PATH'].split(os.pathsep):
        for entry in (Path(folder).iterdir() if Path(folder).is_dir() else ()):
            found.setdefault(entry.name, entry)
    fakes = {'ip': f'#!/bin/bash\n[[ "$1 $2" != "netns add" ]] || exit 1\nexec {IP} "$@"\n',
             'unshare': '#!/bin/bash\nfor arg; do [[ $arg != --net* && $arg != -[!-]*n* ]] || exit 1; done\n'
                        f'exec {found.get("unshare")} "$@"\n'}
    paths = {}
    for kind in ('no-wg', 'no-netns'):
        top = Path(directory) / kind
        top.mkdir()
        for name, entry in found.items():
            if kind == 'no-wg' and name in ('wg', 'wg-quick'):
                continue
            if kind == 'no-netns' and name in fakes:
                (top / name).write_text(fakes[name])
                (top / name).chmod(0o755)
                continue
            (top / name).symlink_to(entry)
        paths[kind] = str(top)
    return paths


if __name__ == '__main__':
    unittest.main()
