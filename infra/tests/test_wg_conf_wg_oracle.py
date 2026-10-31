"""wg0.conf outside apply: the decision and the peers are the ones wg itself ends with, on every edge.

Ten review rounds caught the edges of reading wg0.conf one at a time; values
(`PublicKey = abc`, a bad PSK, ListenPort, keepalive, Endpoint), bytes that are
not UTF-8 and NUL were not seen at all. Here the oracle is wg: a copy of wg-quick
strips the file (bash `read` drops NUL), the real `wg setconf wg0 /dev/stdin`
applies the rest to a fresh wg0 in a temporary netns, `wg show wg0 dump` gives
the peers. The backup check (anysda-backup.sh --check-bundle), verify-traffic.py
(wg_apply_check, restore_wg_apply) and restore (peer cut, server key, live
peers) must end with the same decision and the same peers (key, PSK,
AllowedIPs); a file wg rejects is a complaint naming wg0.conf, not a traceback.
Without wg or a netns to ask, each of them refuses loudly instead of passing the
file. /usr/bin/wg and wg-quick do not read /tmp under AppArmor: the body goes
through stdin, wg-quick runs as a copy. Root via `sudo -n` (wg_root), else skipped.
"""
import base64
import calendar
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_wg_conf_allowedips_prefix import OLD_LINES, VT, conf as prefix_conf, cut, source
from test_wg_conf_lines import forms as line_forms
from test_wg_conf_module import CANONICAL, FORMS, LIB, conf as module_conf, restore_check
from wg_root import as_root

WG, WG_QUICK, IP = shutil.which('wg'), shutil.which('wg-quick'), shutil.which('ip')
S = base64.b64encode(b'0123456789abcdef0123456789abcdef')
A = base64.b64encode(bytes(range(64, 96)))
B = base64.b64encode(bytes(range(96, 128)))
C = base64.b64encode(bytes(range(160, 192)))
STAMP = '2026-10-30T120000Z'


def mk(iface=b'', peer=b'', tail=b'', head=b'', header=b'[Interface]', section=b'[Peer]', eol=b'\n'):
    """[Interface] with the lines `iface`, peer A (PSK) with the lines `peer`, peer B (no PSK), then `tail`."""
    text = (head + header + b'\nListenPort = 51820\nPrivateKey = ' + S + b'\n' + iface +
            b'\n' + section + b'\nPublicKey = ' + A + b'\nPresharedKey = ' + S + b'\nAllowedIPs = 10.66.66.3/32\n' +
            peer + b'\n' + section + b'\nPublicKey = ' + B + b'\nAllowedIPs = 10.66.66.4/32\n' + tail)
    return text.replace(b'\n', eol)


def ips(a=b'AllowedIPs = 10.66.66.3/32, fd66:66::3/128\n', b=b'AllowedIPs = 10.66.66.4/32\n', tail=b''):
    """Two peers with PSK; the AllowedIPs lines of A and B, then `tail` (reviews 4-6)."""
    return (b'[Interface]\nListenPort = 51820\nPrivateKey = ' + S + b'\n\n[Peer]\nPublicKey = ' + A +
            b'\nPresharedKey = ' + S + b'\n' + a + b'\n[Peer]\nPublicKey = ' + B + b'\nPresharedKey = ' + S +
            b'\n' + b + tail)


def allowed(*lines):
    return b''.join(b'AllowedIPs = ' + line + b'\n' for line in lines)


# 1 values wg rejects.
VALUES = {
    'x10-pubkey-bad-format': mk(tail=b'[Peer]\nPublicKey = abc\nAllowedIPs = 10.66.66.9/32\n'),
    'x11-listenport-abc': mk(iface=b'ListenPort = abc\n'),
    'x12-keepalive-abc': mk(peer=b'PersistentKeepalive = abc\n'),
    'x13-endpoint-bad': mk(peer=b'Endpoint = ::::\n'),
    'x14-psk-bad': mk(peer=b'PresharedKey = abc\n'),
    'e25-eq-eq-value': mk(peer=b'PersistentKeepalive == 25\n'),
    'n07-pubkey-abc-eq': mk(tail=b'[Peer]\nPublicKey = abc=\n'),
    'n08-psk-eq-mid': mk(peer=b'PresharedKey = ab=cd\n'),
}
# 2 bytes that are not UTF-8, BOM.
BYTES = {
    'x01-latin1-comment': mk(peer=b'PersistentKeepalive = 25 # caf\xe9\n'),
    'x02-latin1-in-name': mk(peer=b'Persistent\xe9Keepalive = 25\n'),
    'x03-bom': mk(head=b'\xef\xbb\xbf'),
    'x04-latin1-postup': mk(iface=b'PostUp = echo caf\xe9\n'),
    'x15-latin1-in-header': mk(tail=b'[Peer\xe9]\nPublicKey = ' + B + b'\n'),
    'x17-bom-mid': mk(peer=b'\xef\xbb\xbfPersistentKeepalive = 25\n'),
}
# 3 NUL: wg-quick drops it before wg.
NUL = {
    'x05-nul-in-name': mk(peer=b'Persistent\x00Keepalive = 25\n'),
    'x06-nul-in-wgquick-name': mk(iface=b'Add\x00ress = 10.66.66.1/24\n'),
    'x07-nul-after-value': mk(peer=b'PersistentKeepalive = 25\x00Foo\n'),
    'x16-nul-in-header': mk(tail=b'[Peer]\x00x\nPublicKey = ' + A + b'\nAllowedIPs = 10.66.66.9/32\n'),
    'x19-nul-peer-header': mk(tail=b'[Pe\x00er]\nPublicKey = ' + A + b'\nAllowedIPs = 10.66.66.9/32\n'),
}
# 4 files restore cuts peers out of: a lone `\r` inside a line, CRLF, bytes that are not UTF-8.
RESTORE = {
    'e00-plain': mk(),
    'c2-keepalive-cr-eq': mk(peer=b'PersistentKeepalive\r= 25\n'),
    'c3-foo-cr': mk(peer=b'# old\rFoo = 1\n'),
    'c5-privkey-after-cr': mk(iface=b'# old\rPrivateKey = ' + C + b'\n'),
    'c6-pubkey-after-cr': mk(peer=b'# old\rPublicKey = ' + C + b'\n'),
    'e23-crlf': mk(peer=b'PersistentKeepalive = 25\n', eol=b'\r\n'),
    'n16-crlf-whole': mk(iface=b'Address = 10.66.66.1/24\nPostUp = true\n', eol=b'\r\n'),
    'n17-iface-cr-only': mk(header=b'[Interface]\r', iface=b'Address = 10.66.66.1/24\n'),
    'x18-cr-only-eol': mk(peer=b'PersistentKeepalive = 25\r'),
    'x01-latin1-comment': BYTES['x01-latin1-comment'],
    'x04-latin1-postup': BYTES['x04-latin1-postup'],
}
# Edges of the earlier rounds (review 10 probes: edges, new, cr, x, cases).
EARLIER = {
    'x08-emsp-before-address': mk(iface=' Address = 10.66.66.1/24\n'.encode()),
    'x09-saveconfig-noeq': mk(iface=b'SaveConfig\n'),
    'c1-peer-cr-pubkey': mk(tail=b'[Peer]\rPublicKey = ' + B + b'\rAllowedIPs = 10.66.66.9/32\n'),
    'c4-allowed-cr-allowed': mk(peer=b'AllowedIPs = 10.66.66.7/32\rAllowedIPs = 10.66.66.8/32\n'),
    'e01-lower-pubkey': mk(peer=b'publickey = ' + A + b'\n'),
    'e02-upper-allowed': mk(peer=b'ALLOWEDIPS = 10.66.66.9/32\n'),
    'e03-tab-around-eq': mk(peer=b'\tPresharedKey\t=\t' + S + b'\t\n'),
    'e04-space-in-name': mk(peer=b'Persistent Keepalive = 25\n'),
    'e05-space-in-section': mk(section=b'[ Peer ]'),
    'e06-lower-section': mk(section=b'[peer]'),
    'e07-only-eq': mk(peer=b'=\n'),
    'e08-psk-empty': mk(peer=b'PresharedKey =\n'),
    'e09-endpoint-empty': mk(peer=b'Endpoint =\n'),
    'e10-keepalive-empty': mk(peer=b'PersistentKeepalive =\n'),
    'e11-listenport-empty': mk(iface=b'ListenPort =\n'),
    'e12-privkey-empty-2nd': mk(iface=b'PrivateKey =\n'),
    'e13-fwmark-empty': mk(iface=b'FwMark =\n'),
    'e14-comment-after': mk(peer=b'PersistentKeepalive = 25 # keep\n'),
    'e15-address-in-peer': mk(peer=b'Address = 10.66.66.3/32\n'),
    'e16-postup-in-peer': mk(peer=b'PostUp = true\n'),
    'e17-iface-repeat': mk(peer=b'[Interface]\nListenPort = 51821\n'),
    'e18-iface-wgquick': mk(iface=b'Address = 10.66.66.1/24\nPostUp = true\nTable = off\nSaveConfig = false\n'
                                  b'MTU = 1420\nDNS = 1.1.1.1\n'),
    'e19-iface-space-section-addr': mk(header=b'[ Interface ]', iface=b'Address = 10.66.66.1/24\n'),
    'e20-space-in-wgquick-name': mk(iface=b'Post Up = true\n'),
    'e21-address-noeq': mk(iface=b'Address\n'),
    'e22-postup-empty': mk(iface=b'PostUp =\n'),
    'e24-crlf-lower-section': mk(section=b'[pEeR]', eol=b'\r\n'),
    'e26-name-only-key-noeq': mk(peer=b'PresharedKey ' + S + b'\n'),
    'e27-typo-privkey-noeq': mk(iface=b'PrivatKey' + S + b'\n'),
    'e28-privkey-in-peer': mk(peer=b'PrivateKey = ' + S + b'\n'),
    'e29-vtab-around': mk(peer=b'PersistentKeepalive\x0b=\x0c25\n'),
    'e30-address-empty': mk(iface=b'Address =\n'),
    'e31-nbsp-in-name': mk(peer=b'Persistent\xc2\xa0Keepalive = 25\n'),
    'e32-iface-after-peer-wgquick': mk(peer=b'[Interface]\nAddress = 10.66.66.1/24\n'),
    'n01-empty-tab': mk(peer=b'Endpoint =\t\n'),
    'n02-empty-cr': mk(peer=b'Endpoint =\r\n'),
    'n03-empty-vt': mk(peer=b'Endpoint =\x0b\n'),
    'n04-empty-hash': mk(peer=b'Endpoint =#x\n'),
    'n05-empty-hash-sp': mk(iface=b'ListenPort = # c\n'),
    'n06-privkey-tab': mk(iface=b'PrivateKey =\t\n'),
    'n09-mtu-in-peer': mk(peer=b'mtu = 1420\n'),
    'n10-saveconfig-in-peer': mk(peer=b'SaveConfig = false\n'),
    'n11-dns-in-peer': mk(peer=b'DNS = 1.1.1.1\n'),
    'n12-table-in-peer': mk(peer=b'Table = off\n'),
    'n13-iface-sp-after': mk(header=b'[Interface] ', iface=b'Address = 10.66.66.1/24\n'),
    'n14-iface-tab-after': mk(header=b'[Interface]\t', iface=b'Address = 10.66.66.1/24\n'),
    'n15-iface-comment': mk(header='[Interface] # комм'.encode(), iface=b'Address = 10.66.66.1/24\n'),
    'n18-iface-repeat-addr': mk(head=b'[Interface]\n', iface=b'Address = 10.66.66.1/24\n'),
    'n19-iface-repeat-after-peer': mk(tail=b'[Interface]\nAddress = 10.66.66.1/24\nMTU = 1420\n'),
    'n20-peer-eq-header': mk(tail=b'[Peer]=x\nPublicKey = ' + B + b'\n'),
    'n21-iface-eq-header': mk(header=b'[Interface]='),
    'n22-iface-eq-header-addr': mk(header=b'[Interface]=x', iface=b'Address = 10.66.66.1/24\n'),
    'n23-iface-upper': mk(header=b'[INTERFACE]', iface=b'Address = 10.66.66.1/24\n'),
    'n24-iface-vt-before': mk(header=b'\x0b[Interface]', iface=b'Address = 10.66.66.1/24\n'),
    'n25-iface-vt-after': mk(header=b'[Interface]\x0b', iface=b'Address = 10.66.66.1/24\n'),
    'n26-addr-vt-name': mk(iface=b'Address\x0b= 10.66.66.1/24\n'),
    'n27-addr-lead-sp': mk(iface=b'  \tAddress = 10.66.66.1/24\n'),
    'n28-post-tab-up': mk(iface=b'Post\tUp = true\n'),
    'n29-addr-hash-empty': mk(iface=b'Address =#x\n'),
    'n30-wgquick-before-section': mk(head=b'Address = 10.66.66.1/24\n'),
    'n31-mtu-noeq': mk(iface=b'MTU\n'),
    'n32-eq-name-empty': mk(iface=b'= 5\n'),
    'n33-addr-cr-name': mk(iface=b'Address\r= 10.66.66.1/24\n'),
    'n34-iface-hash-noeq-addr': mk(header=b'[Interface]#', iface=b'Address = 10.66.66.1/24\n'),
    'n35-peer-sp-header': mk(tail=b'[ Peer ]=x\nPublicKey = ' + B + b'\n'),
    'n36-iface-nbsp-after': mk(header=b'[Interface]\xc2\xa0', iface=b'Address = 10.66.66.1/24\n'),
    'n37-ff-empty': mk(peer=b'Endpoint =\x0c\n'),
    'p1-comma-mixed': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32, -10.66.66.9/32, +fd66:66::3/128')),
    'p1-hash-after': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32', b'-10.66.66.9/32 # drop')),
    'p1-hash-before': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32 # , -10.66.66.9/32')),
    'p1-minus-absent': ips(a=allowed(b'10.66.66.3/32', b'-10.66.66.9/32')),
    'p1-minus-after': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32', b'-10.66.66.9/32')),
    'p1-minus-before': ips(a=allowed(b'-10.66.66.3/32', b'10.66.66.3/32')),
    'p1-plus': ips(a=allowed(b'+10.66.66.3/32, +fd66:66::3/128')),
    'p1-sign-then-hash': ips(a=allowed(b'10.66.66.3/32, -#10.66.66.3/32')),
    'p1-space-sign': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32', b'- 10.66.66.9/32')),
    'p1-v6-minus': ips(a=allowed(b'10.66.66.3/32, fd66:66::3/128', b'-fd66:66::3/128')),
    'p2-leading-zero': ips(a=allowed(b'10.66.66.3/32', b'-10.66.66.03/32')),
    'p2-mask-zero': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32', b'-10.66.66.9/032')),
    'p2-nomask-add': ips(a=allowed(b'10.66.66.3')),
    'p2-nomask-minus': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32', b'-10.66.66.9')),
    'p2-only-removed': ips(a=allowed(b'10.66.66.3/32', b'-10.66.66.3')),
    'p2-v6-form-dup': ips(a=allowed(b'10.66.66.3/32, fd66:66::3/128, fd66:66:0::3/128')),
    'p2-v6-form': ips(a=allowed(b'10.66.66.3/32, fd66:66::3/128', b'-fd66:66:0::3/128')),
    'q-default-both': ips(a=allowed(b'0.0.0.0/0, ::/0'), b=allowed(b'0.0.0.0/0')),
    'q-default-routes': ips(a=allowed(b'0.0.0.0/0, ::/0')),
    'q-hostbits-minus': ips(a=allowed(b'10.66.66.0/24', b'-10.66.66.3/24')),
    'q-hostbits-v6': ips(a=allowed(b'fd66:66::3/64')),
    'q-hostbits': ips(a=allowed(b'10.66.66.3/24')),
    'q-minus-other-ab': ips(a=allowed(b'10.66.66.3/32, -10.66.66.4/32')),
    'q-minus-other': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32'), b=allowed(b'10.66.66.4/32, -10.66.66.9/32')),
    'q-two-then-minus': ips(a=allowed(b'10.66.66.3/32, 10.66.66.9/32'),
                            b=allowed(b'10.66.66.4/32, 10.66.66.9/32', b'-10.66.66.9/32')),
    'q-v6-minus-form': ips(a=allowed(b'10.66.66.3/32, fd66:66::9/128'),
                           b=allowed(b'10.66.66.4/32, -FD66:66:0::9/128')),
    'q-v6-two-forms': ips(a=allowed(b'10.66.66.3/32, fd66:66::9/128'), b=allowed(b'10.66.66.4/32, fd66:66:0:0::9')),
    'r-double-comma': ips(a=allowed(b'10.66.66.3/32,,10.66.66.9/32')),
    'r-double-sign': ips(a=allowed(b'10.66.66.3/32, +-10.66.66.9/32')),
    'r-empty-first': ips(a=b'AllowedIPs =\n' + allowed(b'10.66.66.3/32')),
    'r-empty-line': ips(a=allowed(b'10.66.66.3/32') + b'AllowedIPs =\n'),
    'r-empty-only': ips(a=b'AllowedIPs =\n'),
    'r-leading-comma': ips(a=allowed(b',10.66.66.3/32')),
    'r-mask-33': ips(a=allowed(b'10.66.66.3/33')),
    'r-mask-empty': ips(a=allowed(b'10.66.66.3/')),
    'r-mask-long0': ips(a=allowed(b'10.66.66.3/0000000032')),
    'r-mask-unicode': ips(a=allowed('10.66.66.3/٣٢'.encode())),
    'r-mask-v6-129': ips(a=allowed(b'fd66::3/129')),
    'r-sign-only': ips(a=allowed(b'10.66.66.3/32, -')),
    'r-trailing-comma': ips(a=allowed(b'10.66.66.3/32,')),
    'r-unicode-digit': ips(a=allowed('10.66.66.٣/32'.encode())),
    'r-v4-hex': ips(a=allowed(b'0x0a.66.66.3/32')),
    'r-v4-short': ips(a=allowed(b'10.66.3/32')),
    'r-v4mapped': ips(a=allowed(b'::ffff:10.66.66.3/128')),
    'r-v6-v4tail': ips(a=allowed(b'fd66::10.66.66.3/128')),
    'r-v6-zone': ips(a=allowed(b'fe80::1%eth0/128')),
    's-dup-peer-plus': ips(a=allowed(b'10.66.66.3/32'), tail=b'\n[Peer]\nPublicKey = ' + A + b'\nPresharedKey = ' +
                           S + b'\nAllowedIPs = +10.66.66.9/32\n'),
    's-dup-peer': ips(a=allowed(b'10.66.66.3/32'), tail=b'\n[Peer]\nPublicKey = ' + A + b'\nPresharedKey = ' +
                      S + b'\nAllowedIPs = 10.66.66.9/32\n'),
    't-empty-pubkey': ips(a=allowed(b'10.66.66.3/32'), tail=b'PublicKey =\n'),
    't-peer-nokey': ips(a=allowed(b'10.66.66.3/32') + b'[Peer]\nAllowedIPs = 10.66.66.9/32\n'),
    't-pubkey-blank-after': ips(a=allowed(b'10.66.66.3/32') + b'[Peer]\nPublicKey =\nAllowedIPs = 10.66.66.9/32\n'),
    'x-cross-peer': ips(a=allowed(b'10.66.66.3/32, 10.66.66.4/32')),
    'z-canonical': ips(),
    'z-emptied': ips(a=allowed(b'10.66.66.3/32', b'-10.66.66.3/32')),
}


def own_files():
    """The node's own files (28-wireguard.sh header, backend wg.apply) and the forms of the earlier tests."""
    found = {f'lines-{place}-{form}': text.encode() for (place, form), text in line_forms().items()}
    found |= {f'module-{form}': module_conf(sections).encode() for form, sections in FORMS.items()}
    found |= {f'prefix-{form}': prefix_conf(form).encode() for form in OLD_LINES}
    found['module-canonical'] = module_conf(CANONICAL).encode()
    return found


_names = itertools.count()


def ns_wg(body, *show):
    """`wg setconf wg0 /dev/stdin` of `body` on a fresh wg0 in a temporary netns: (error or None, dump lines)."""
    name = f'vpn2t12o{os.getpid()}x{next(_names)}'
    subprocess.run([IP, 'netns', 'add', name], check=True, capture_output=True)
    try:
        subprocess.run([IP, '-n', name, 'link', 'add', 'wg0', 'type', 'wireguard'], check=True, capture_output=True)
        done = subprocess.run([IP, 'netns', 'exec', name, WG, 'setconf', 'wg0', '/dev/stdin'], input=body,
                              capture_output=True, check=False)
        if done.returncode:
            return done.stderr.decode(errors='replace').strip() or 'rejected', []
        dump = subprocess.run([IP, 'netns', 'exec', name, WG, 'show', 'wg0', *(show or ('dump',))], check=True,
                              capture_output=True).stdout.decode()
        return None, dump.splitlines()
    finally:
        subprocess.run([IP, 'netns', 'del', name], check=False, capture_output=True)


def oracle(raw):
    """wg-quick strip (a copy of the script) + wg: (error or None, private key, sorted [(key, PSK, AllowedIPs)])."""
    with tempfile.TemporaryDirectory() as directory:
        top = Path(directory)
        shutil.copy(WG_QUICK, top / 'wg-quick')
        (top / 'wg0.conf').write_bytes(raw)
        (top / 'wg0.conf').chmod(0o600)
        strip = subprocess.run(['bash', str(top / 'wg-quick'), 'strip', str(top / 'wg0.conf')],
                               capture_output=True, check=False)
    if strip.returncode:
        return 'wg-quick: ' + strip.stderr.decode(errors='replace').strip(), None, None
    error, dump = ns_wg(strip.stdout)
    if error:
        return error, None, None
    peers = []
    for line in dump[1:]:
        key, psk, _endpoint, ips_ = line.split('\t')[:4]
        peers.append((key, '' if psk == '(none)' else psk, sorted([] if ips_ == '(none)' else ips_.split(','))))
    return None, dump[0].split('\t')[0], sorted(peers)


def backup(raw, rows):
    """anysda-backup.sh --check-bundle on wg0.conf `raw`; rows: (key, PSK, wg_ip, transitional) -> (code, lines)."""
    with tempfile.TemporaryDirectory() as directory:
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
        done = subprocess.run(['bash', str(LIB / 'anysda-backup.sh'), '--check-bundle', str(root)],
                              env=os.environ | {'ANYSDA_LIB_DIR': str(LIB)}, capture_output=True, text=True,
                              timeout=120, check=False)
    return done.returncode, done.stdout.splitlines() + done.stderr.splitlines()


def through(stub):
    """VT.run with `wg show wg0 peers` and `wg syncconf wg0` answered by `stub`, every other command real."""
    real = VT.run

    def run(*args, **kwargs):
        if args[:3] in (('wg', 'show', 'wg0'), ('wg', 'syncconf', 'wg0')):
            return stub(*args, **kwargs)
        return real(*args, **kwargs)
    return run


def gate(raw, live):
    """verify-traffic.py wg_apply_check() on wg0.conf `raw`, live wg0 holding `live`: [(status, note)]."""
    notes = []

    class Report:
        @staticmethod
        def add(_label, _component, status, note):
            notes.append((status, note))

    class Done:
        stdout = ' '.join(live)

    saved = VT.WG_CONF, VT.CONTROL_SOCKET, VT.CONTROL_WG_STATE, VT.run, VT.WireGuard.free_addresses
    with tempfile.TemporaryDirectory() as directory:
        VT.WG_CONF, VT.CONTROL_SOCKET = Path(directory) / 'wg0.conf', Path(directory)
        VT.CONTROL_WG_STATE = Path(directory) / 'absent.json'
        VT.WG_CONF.write_bytes(raw)
        VT.run = through(lambda *_args, **_kw: Done)
        VT.WireGuard.free_addresses = staticmethod(lambda _count: [])
        try:
            VT.wg_apply_check(Report)
        finally:
            VT.WG_CONF, VT.CONTROL_SOCKET, VT.CONTROL_WG_STATE, VT.run = saved[:4]
            VT.WireGuard.free_addresses = staticmethod(saved[4])
    return notes


def syncconf_peers(raw):
    """verify-traffic.py restore_wg_apply() on a saved wg0.conf `raw`: the peers it sends to `wg syncconf`."""
    sent = []
    saved = VT.WORK, VT.run, VT.restore_file
    with tempfile.TemporaryDirectory() as directory:
        VT.WORK = Path(directory)
        (VT.WORK / 'wg0.conf').write_bytes(raw)
        VT.run = through(lambda *_args, data=None, **_kw: sent.append(data))
        VT.restore_file = lambda *_args: None
        try:
            VT.restore_wg_apply()
        finally:
            VT.WORK, VT.run, VT.restore_file = saved
    assert len(sent) == 1, sent
    peers = []
    for section in sent[0].split('[Peer]')[1:]:
        values = {}
        for line in section.split('\n'):
            name, _, value = line.partition('=')
            if name.strip():
                values.setdefault(name.strip().lower(), []).append(value.strip())
        peers.append((values['publickey'][-1], (values.get('presharedkey') or [''])[-1],
                      sorted(entry.strip() for line in values.get('allowedips', []) for entry in line.split(','))))
    return sorted(peers)


RESTORE_SH = source('infra/lib/anysda-restore.sh')
SCOPE = {'hashlib': hashlib, 'os': os, 're': re, 'shutil': shutil, 'sqlite3': sqlite3, 'subprocess': subprocess,
         'sys': sys, 'tempfile': tempfile}


def restore_cut(raw, keys):
    """anysda-restore.sh: the cut of the peers `keys` out of the bundle's wg0.conf, twice: (bytes, bytes)."""
    block = cut(RESTORE_SH, 'def rewrite(', '\nif (ike_ids')
    out = []
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'wg0.conf'
        path.write_bytes(raw)
        for _ in range(2):
            exec(block, dict(SCOPE, wg_keys=set(keys), wg_path=str(path)))  # noqa: S102 - the script's own code
            out.append(path.read_bytes())
    return out


def restore_server_key(raw):
    """anysda-restore.sh wg_key(): the digest of the server key it reads from wg0.conf."""
    scope = dict(SCOPE)
    exec(cut(RESTORE_SH, 'def digest(', '\n\ndef index_rows'), scope)  # noqa: S102
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / 'wg0.conf').write_bytes(raw)
        return scope['wg_key'](directory)


def restore_live_peers(raw):
    """anysda-restore.sh: the public keys it reads from the live wg0.conf when the live db is not read."""
    block = cut(RESTORE_SH, 'def live_text(', '\nif live_ike is not None:')
    assert block.count("'/etc/wireguard/wg0.conf'") == 1, block
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'wg0.conf'
        path.write_bytes(raw)
        scope = dict(SCOPE, live_read=False)
        exec(block.replace("'/etc/wireguard/wg0.conf'", repr(str(path))), scope)  # noqa: S102
    return scope['live_peers']


def expected_cut(raw, key):
    """The bytes of `raw` without the [Peer] blocks whose last PublicKey is `key`, the rest byte for byte."""
    blocks = [[]]
    for line in re.findall(rb'[^\n]*\n|[^\n]+', raw):
        clean = re.sub(rb'[ \t\n\v\f\r]', b'', line.split(b'#', 1)[0])
        if clean.startswith(b'['):
            blocks.append([])
        blocks[-1].append((clean, line))
    kept = b''
    for block in blocks:
        keys = [clean.partition(b'=')[2] for clean, _ in block if clean.partition(b'=')[0].lower() == b'publickey']
        if not (block and block[0][0].lower() == b'[peer]' and keys and keys[-1] == key.encode()):
            kept += b''.join(line for _, line in block)
    return kept


def without_wg(directory):
    """PATH dirs as now: one without wg and wg-quick, one whose `ip netns add` is refused."""
    found = {}
    for folder in os.environ['PATH'].split(os.pathsep):
        for entry in (Path(folder).iterdir() if Path(folder).is_dir() else ()):
            found.setdefault(entry.name, entry)
    paths = {}
    for kind in ('no-wg', 'no-netns'):
        top = Path(directory) / kind
        top.mkdir()
        for name, entry in found.items():
            if kind == 'no-wg' and name in ('wg', 'wg-quick'):
                continue
            if kind == 'no-netns' and name == 'ip':
                (top / 'ip').write_text('#!/bin/bash\n[[ "$1 $2" != "netns add" ]] || '
                                        '{ echo "mount --make-shared /run/netns failed: Operation not permitted" >&2; '
                                        f'exit 1; }}\nexec {IP} "$@"\n')
                (top / 'ip').chmod(0o755)
                continue
            (top / name).symlink_to(entry)
        paths[kind] = str(top)
    return paths


@as_root
@unittest.skipUnless(WG and WG_QUICK and IP, 'needs wg, wg-quick and ip')
class WgOracleTests(unittest.TestCase):
    """1 values, 2 not UTF-8, 3 NUL, 4 restore, 5 peers on every edge, 6 nothing to ask, 7 own files."""

    def check(self, name, raw):
        """Decision and peers of the backup check and verify against wg on one file."""
        error, _private, peers = oracle(raw)
        if error:
            code, lines = backup(raw, [(A.decode(), S.decode(), '10.66.66.3', False)])
            with self.subTest('backup: exit 3 naming wg0.conf, like wg rejects', edge=name, wg=error):
                self.assertEqual(code, 3, lines)
                self.assertTrue([line for line in lines if 'wg0.conf' in line], lines)
                self.assertFalse([line for line in lines if 'Traceback' in line], lines)
            with self.subTest('verify wg_apply_check: FAIL naming wg0.conf', edge=name, wg=error):
                notes = gate(raw, [A.decode(), B.decode()])
                self.assertTrue(notes and notes[0][0] == VT.FAIL and 'wg0.conf' in notes[0][1], notes)
            with self.subTest('verify restore_wg_apply: refuses naming wg0.conf', edge=name, wg=error):
                with self.assertRaises(ValueError) as caught:
                    syncconf_peers(raw)
                self.assertIn('wg0.conf', str(caught.exception))
            return
        # Every peer known only by the address it holds in wg: a peer of the copy wg does not have,
        # or without that address, is a complaint.
        rows = [(base64.b64encode(bytes([200 + number] * 32)).decode(), 'x', v4[0][:-3], True) if v4 else
                (key, psk or 'x', '', True)
                for number, (key, psk, nets) in enumerate(peers)
                for v4 in [[net for net in nets if re.fullmatch(r'[0-9.]+/32', net)]]]
        with self.subTest('backup: the peers and their addresses are wg\'s', edge=name):
            self.assertEqual(backup(raw, rows), (0, []))
        # Every peer an active device with its PSK; a peer without PSK is a device without one (disabled).
        rows = [(key, psk, f'10.99.0.{number + 1}', False) for number, (key, psk, _) in enumerate(peers)]
        code, lines = backup(raw, rows)
        disabled = [f'«d{number}»' for number, (_, psk, _) in enumerate(peers, 1) if not psk]
        with self.subTest('backup: the keys and PSKs are wg\'s', edge=name):
            self.assertEqual((code, len(lines)), (3 if disabled else 0, len(disabled)), lines)
            for label in disabled:
                self.assertTrue([line for line in lines if label in line], (label, lines))
        with self.subTest('verify wg_apply_check: no FAIL', edge=name):
            notes = gate(raw, [key for key, _, _ in peers])
            self.assertFalse([note for note in notes if note[0] == VT.FAIL], notes)
        with self.subTest('verify restore_wg_apply: the peers are wg\'s', edge=name):
            self.assertEqual(syncconf_peers(raw), peers)

    def check_all(self, table):
        for name, raw in table.items():
            self.check(name, raw)

    def test_0_oracle(self):
        error, private, peers = oracle(mk())
        self.assertEqual((error, private, [key for key, _, _ in peers]), (None, S.decode(), sorted([A.decode(), B.decode()])))
        self.assertIn('abc', oracle(VALUES['x10-pubkey-bad-format'])[0])
        self.assertIsNone(oracle(NUL['x05-nul-in-name'])[0])
        self.assertIsNotNone(ns_wg(NUL['x05-nul-in-name'])[0])

    def test_1_values_wg_rejects(self):
        for name, raw in VALUES.items():
            with self.subTest('wg rejects', edge=name):
                self.assertIsNotNone(oracle(raw)[0])
        self.check_all(VALUES)

    def test_2_not_utf8_and_bom(self):
        self.check_all(BYTES)

    def test_3_nul_as_wg_quick_and_wg(self):
        self.check_all(NUL)

    def test_4_restore_cut_server_key_live_peers(self):
        for name, raw in RESTORE.items():
            error, private, peers = oracle(raw)
            self.assertIsNone(error, name)
            with self.subTest('restore: server key is the one wg takes', edge=name):
                self.assertEqual(restore_server_key(raw), hashlib.sha256(private.encode()).hexdigest())
            with self.subTest('restore: live peers are wg\'s', edge=name):
                self.assertEqual(restore_live_peers(raw), {key for key, _, _ in peers})
            for key, _, _ in peers:
                with self.subTest('restore: peer cut', edge=name, peer=key[:4]):
                    try:
                        first, second = restore_cut(raw, {key})
                    except Exception as caught:  # noqa: BLE001 - the restore step must not die on the file
                        self.fail(f'{type(caught).__name__}: {caught}')
                    self.assertEqual(first, expected_cut(raw, key))
                    self.assertEqual(second, first)
                    error_after, _, peers_after = oracle(first)
                    self.assertIsNone(error_after)
                    self.assertEqual(peers_after, [peer for peer in peers if peer[0] != key])

    def test_5_peers_on_every_edge(self):
        self.check_all(RESTORE | EARLIER)

    def test_6_nothing_to_ask_is_a_loud_refusal(self):
        text = module_conf(CANONICAL)
        with tempfile.TemporaryDirectory() as directory:
            for kind, path in without_wg(directory).items():
                with patch.dict(os.environ, {'PATH': path}):
                    code, lines = backup(mk(), [(A.decode(), S.decode(), '10.66.66.3', False),
                                                (B.decode(), '', '10.66.66.4', False)])
                    with self.subTest('backup: refuses naming wg0.conf', kind=kind):
                        self.assertNotEqual(code, 0, lines)
                        self.assertTrue([line for line in lines if 'wg0.conf' in line], lines)
                    with self.subTest('verify wg_apply_check: FAIL naming wg0.conf', kind=kind):
                        notes = gate(mk(), [A.decode(), B.decode()])
                        self.assertTrue(notes and notes[0][0] in (VT.FAIL, VT.UNKNOWN) and 'wg0.conf' in notes[0][1],
                                        notes)
                    with self.subTest('verify restore_wg_apply: refuses naming wg0.conf', kind=kind):
                        with self.assertRaises(Exception) as caught:
                            syncconf_peers(mk())
                        self.assertIn('wg0.conf', str(caught.exception))
                    with self.subTest('restore: the check warns, naming wg0.conf', kind=kind):
                        _code, err = restore_check(text)
                        self.assertIn('WARN', err)
                        self.assertIn('wg0.conf', err)

    def test_7_own_files_and_earlier_forms(self):
        own = own_files()
        for name in ('lines-written-written', 'module-canonical'):
            with self.subTest('own file: wg accepts', edge=name):
                self.assertIsNone(oracle(own[name])[0])
        self.check_all(own)


if __name__ == '__main__':
    unittest.main()
