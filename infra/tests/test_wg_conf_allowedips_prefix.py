"""wg0.conf forms with `+`/`-` AllowedIPs entries and the installed-wg helpers, shared by the wg0.conf tests."""
import base64
import os
from pathlib import Path
import re

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))

SERVER = base64.b64encode(bytes(range(32))).decode()
PEER = base64.b64encode(bytes(range(64, 96))).decode()
GONE = base64.b64encode(bytes(range(96, 128))).decode()
OLD = base64.b64encode(bytes(range(128, 160))).decode()
NEW = base64.b64encode(bytes(range(160, 192))).decode()
PSK_ONE, PSK_GONE, PSK_TWO = (base64.b64encode(bytes([n] * 32)).decode() for n in (1, 2, 3))

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






def source(path):
    return (ROOT / path).read_text()


def cut(text, start, end):
    assert start in text, start
    body = text.split(start, 1)[1]
    assert end in body, end
    return start + body.split(end, 1)[0]












WG_APPLY = [{'key': PEER, 'preshared_key': PSK_ONE, 'ip': '10.66.66.2'},
            {'key': GONE, 'preshared_key': PSK_GONE, 'ip': '10.66.66.4'},
            {'key': OLD, 'preshared_key': PSK_TWO, 'ip': '10.66.66.3'}]
