"""wg0.conf forms with AllowedIPs read as networks and the reference wg_sets, shared by the wg0.conf tests."""
import ipaddress
import re

from test_wg_conf_allowedips_prefix import (
    A4, A6, B4, GONE, OLD, PEER, PSK_GONE, PSK_ONE, PSK_TWO, SERVER)

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
# (Linux 7.0, wireguard-tools 1.0.20250521): {peer: set}. 1.0.20210914 rejects every form with a signed entry.
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
