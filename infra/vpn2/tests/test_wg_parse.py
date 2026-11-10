"""vpn2.wg: разбор wg0.conf без внешних программ. Входы - формы из infra/tests, ожидания сняты со старого кода."""
import base64
import re

import pytest

from vpn2 import wg
from vpn2.wg import WgError, WgUnchecked


def make_key(seed):
    return base64.b64encode(bytes((seed * 7 + i) % 256 for i in range(32))).decode()


A, B, C, S = (make_key(n) for n in (1, 2, 3, 4))
HEADER = f'[Interface]\nAddress = 192.0.2.1/24\nListenPort = 51820\nPrivateKey = {S}\n'


def peer(key, extra='AllowedIPs = 192.0.2.2/32\n'):
    return f'\n[Peer]\nPublicKey = {key}\n{extra}'


def refused(text, fragment):
    with pytest.raises(WgError, match=re.escape(fragment)):
        wg.check(text)


def test_error_classes():
    assert issubclass(WgError, ValueError)
    assert issubclass(WgUnchecked, WgError)
    assert not issubclass(WgError, WgUnchecked)


def test_valid_key_modes():
    assert wg.valid_key(A) and wg.valid_key(A, nonzero=True)
    assert wg.KEY and re.fullmatch(wg.KEY, A)
    # не канонический: последний знак с лишними битами. strict=False его берёт, strict - нет
    loose = A[:-2] + 'B='
    assert not wg.valid_key(loose) and wg.valid_key(loose, strict=False)
    assert not wg.valid_key(loose, nonzero=True, strict=False)
    zero = base64.b64encode(bytes(32)).decode()
    assert wg.valid_key(zero) and not wg.valid_key(zero, nonzero=True)
    for bad in ('', 'x' * 43, A[:-1], A + '=', A[:-1] + '\n', None, 5, b'a' * 44, 'Я' * 43 + '='):
        assert not wg.valid_key(bad)
        assert not wg.valid_key(bad, strict=False)


def test_quick_strip_drops_wg_quick_names():
    data = b'[Interface]\nAddress = 192.0.2.1/24\nDNS=1.1.1.1\nListenPort = 1\n[Peer]\nAddress = 1\n'
    assert wg.quick_strip(data) == [(1, b'[Interface]'), (4, b'ListenPort = 1'), (5, b'[Peer]'), (6, b'Address = 1')]
    assert wg.quick_strip(data) == wg.quick_strip(data)


def test_quick_strip_nul_case_and_edges():
    assert wg.quick_strip(b'') == [] and wg.quick_strip(b'# c\n') == [(1, b'# c')]
    # NUL вон, имя и заголовок в любом регистре, края обрезаны
    assert wg.quick_strip(b'\t[INTERFACE]\x00 \nADD\x00RESS\t=\t1\nPort=2') == [(1, b'[INTERFACE]'), (3, b'Port=2')]
    # кириллическая «е» в имени - не wg-quick, строка остаётся wg
    assert wg.quick_strip(b'[Interface]\nAddr\xd0\xb5ss = 1\n') == [(1, b'[Interface]'), (2, b'Addr\xd0\xb5ss = 1')]


@pytest.mark.parametrize('value', [b'true', b'FALSE', b' tRue # c'])
def test_quick_strip_saveconfig_ok(value):
    assert wg.quick_strip(b'[Interface]\nSaveConfig = ' + value + b'\n') == [(1, b'[Interface]')]


@pytest.mark.parametrize('value', [b'maybe', b'', b'1', b'#true'])
def test_quick_strip_saveconfig_refused(value):
    with pytest.raises(WgError, match='SaveConfig'):
        wg.quick_strip(b'[Interface]\nSaveConfig = ' + value + b'\n')


def test_quick_strip_saveconfig_outside_interface_is_wgs():
    assert wg.quick_strip(b'[Peer]\nSaveConfig = maybe\n') == [(1, b'[Peer]'), (2, b'SaveConfig = maybe')]


@pytest.mark.parametrize('text, expected', [
    ('[Interface]', ('[Interface]', '')),
    (' [ peer ] # c', ('[Peer]', '')),
    (f'PublicKey = {A}', ('PublicKey', A)),
    (f'pUbLiCkEy\t=\t{A} # c', ('PublicKey', A)),
    ('ListenPort=51820', ('ListenPort', '51820')),
    ('Address = 1', (None, 'Address=1')),
    ('# only a comment', (None, '')),
    ('', (None, '')),
    # NBSP и \x85 wg читает как текст, а не как пробел
    (f'Public\xa0Key={A}', (None, f'Public\xa0Key={A}')),
    ('Публичный=1', (None, 'Публичный=1')),
    ('[Interface]x', (None, '[Interface]x')),
])
def test_line(text, expected):
    assert wg.line(text) == expected


def test_split_header_and_tail():
    text = HEADER + peer(A) + peer(B)
    header, tail = wg.split(text)
    assert header == HEADER
    assert tail == '[Peer]' + text.split('[Peer]', 1)[1]
    assert wg.split(text) == wg.split(text)


def test_split_without_peers_and_empty():
    assert wg.split(HEADER + '\n\n') == (HEADER, '')
    assert wg.split('') == ('\n', '')
    assert wg.split('# c') == ('# c\n', '')


def test_split_comment_above_peer_goes_with_it():
    text = HEADER + f'\n# peer one\n  # more\n[Peer]\nPublicKey = {A}\n'
    header, tail = wg.split(text)
    assert header == HEADER
    assert tail == f'# peer one\n  # more\n[Peer]\nPublicKey = {A}\n'


def test_split_blank_lines_and_latin1_bytes():
    text = HEADER + f'\n\n[Peer] \nPublicKey = {A}\n# caf\xe9 \xff\n'
    header, tail = wg.split(text)
    assert header == HEADER
    assert tail == f'[Peer] \nPublicKey = {A}\n# caf\xe9 \xff\n'
    assert tail.encode('latin-1').endswith(b'caf\xe9 \xff\n')


def test_check_accepts_base_files():
    wg.check(HEADER)
    wg.check(HEADER + peer(A) + peer(B, f'PresharedKey = {C}\nAllowedIPs = 192.0.2.3/32, fd00::3/128\n'))
    wg.check(HEADER.replace('Address', 'address') + 'SaveConfig = TRUE\nFwMark = off\n# c\n')
    wg.check(f'[interface]\r\nlistenport=1\r\nPRIVATEKEY={S}\r\n')
    wg.check(HEADER + 'PostUp = iptables -A x # c\n' + peer(A, 'AllowedIPs = -192.0.2.9/32\n'))


@pytest.mark.parametrize('text, fragment', [
    ('', 'PrivateKey missing'),
    ('# only comments\n\n', 'PrivateKey missing'),
    (f'[Interface]\nPrivateKey = {S}\n', 'ListenPort missing'),
    (HEADER.replace('51820', '0'), 'ListenPort missing'),
    (HEADER.replace('51820', '70000'), 'ListenPort missing'),
    (HEADER + peer(A, f'PublicKey = {B}\n'), '[Peer] without one valid PublicKey'),
    (HEADER + '[Peer]\nAllowedIPs = 192.0.2.2/32\n', '[Peer] without one valid PublicKey'),
    (HEADER + 'PublicKey = ' + A + '\n', 'PublicKey in [Interface]'),
    (f'PrivateKey = {S}\n', 'PrivateKey in no section'),
    (HEADER.replace(S, S[:-2] + 'B='), 'PrivateKey: wg cannot read'),
    (HEADER.replace(S, ''), 'PrivateKey: wg cannot read'),
    (HEADER + 'FwMark = 0x100000000\n', 'FwMark: wg takes'),
    (HEADER + 'FwMark = 0X10\n', 'FwMark: wg cannot read'),
    (HEADER + 'junk\n', 'wg cannot read'),
    (HEADER + 'Endpoint = 192.0.2.1:1\n', 'wg cannot read'),
    (HEADER + 'SaveConfig = maybe\n', 'SaveConfig: wg-quick takes true or false'),
    (HEADER + 'Public\xa0Key = x\n', 'wg cannot read'),
    ('[Interface]\nListen\x00Port=1\n', 'wg cannot read'),
    (HEADER + peer(A, 'AllowedIPs = 192.0.2.2/32,\n'), 'AllowedIPs: wg cannot read'),
])
def test_check_refusals(text, fragment):
    refused(text, fragment)


def test_check_interface_after_peer_names_path_and_number():
    text = HEADER + peer(A) + peer(B) + '[Interface]\nListenPort = 2\n'
    with pytest.raises(WgError, match=r'/etc/wireguard/wg0\.conf: .*after \[Peer\] number 2'):
        wg.check(text)


def test_check_ignores_wg_quick_names_outside_peer_tail_only():
    # Address после [Peer] - строка wg, не wg-quick: в хвосте пиров её не проверяют
    wg.check(HEADER + peer(A, 'Endpoint = x\n'))
    refused(HEADER.replace('[Interface]\n', '[Interface]\nTab\t= 1\n'), 'wg cannot read')


def test_check_non_utf8_latin1_comment_and_repeat():
    text = HEADER + '# caf\xe9 \xff\xc3\xa9\n' + peer(A)
    wg.check(text)
    wg.check(text)
    with pytest.raises(WgError) as first:
        wg.check('')
    with pytest.raises(WgError) as second:
        wg.check('')
    assert str(first.value) == str(second.value)


def test_syncconf_body_keeps_only_wg_lines_in_order():
    text = HEADER + 'SaveConfig = true\nPostUp = x\n# note\n' + peer(A, f'PresharedKey = {C}\nAllowedIPs = 192.0.2.2/32\n')
    body = wg.syncconf_body(text)
    assert body == (f'[Interface]\nListenPort = 51820\nPrivateKey = {S}\n'
                    f'[Peer]\nPublicKey = {A}\nPresharedKey = {C}\nAllowedIPs = 192.0.2.2/32\n')
    assert wg.syncconf_body(text) == body


def test_syncconf_body_comments_with_keys_and_no_peers():
    text = f'[interface]\nprivatekey = {S} # mine\nListenPort=1\n# c\nAddress=1\n'
    assert wg.syncconf_body(text) == f'[interface]\nprivatekey = {S} # mine\nListenPort=1\n\n'
    assert wg.syncconf_body('') == '\n\n'


def test_private_key_interface_only_vs_any_section():
    text = HEADER + peer(A, f'PrivateKey = {B}\n')
    assert wg.private_key(text) == S
    assert wg.private_key(text, interface_only=False) == B
    only_peer = f'[Peer]\nPrivateKey = {B}\n'
    assert wg.private_key(only_peer) is None
    assert wg.private_key(only_peer, interface_only=False) == B


def test_private_key_last_wins_and_blank_forms():
    text = f'[Interface]\nPrivateKey={A}\n  privatekey\t=\t{B} # c\n'
    assert wg.private_key(text) == B and wg.private_key(text, interface_only=False) == B
    assert wg.private_key('') is None and wg.private_key('', interface_only=False) is None
    assert wg.private_key('# PrivateKey = x\n') is None
    # NUL внутри имени и пробелы внутри ключа: wg их не видит
    assert wg.private_key(f'[Interface]\nPriv\x00ateKey = {A[:10]} {A[10:]}\n') == A
    # пустое значение в awk сбрасывает ключ, в wg_key пустые просто пропускаются
    reset = f'[Interface]\nPrivateKey = {A}\nPrivateKey =\n'
    assert wg.private_key(reset) is None
    assert wg.private_key(reset, interface_only=False) == A


def test_private_key_other_section_between_interfaces():
    text = f'[Interface]\nPrivateKey={A}\n[Peer]\nPrivateKey={B}\n[Interface]\nListenPort=1\n'
    assert wg.private_key(text) == A
    assert wg.private_key(text, interface_only=False) == B
    assert wg.private_key(f'[Interface] x\nPrivateKey={A}\n') is None


def test_public_keys():
    text = HEADER + peer(A) + peer(B) + peer(A) + '# PublicKey = ' + C + '\nPublicKey =\n'
    assert wg.public_keys(text) == {A, B}
    assert wg.public_keys('') == set() and wg.public_keys(HEADER) == set()
    assert wg.public_keys(f'[Peer]\nPUBLICKEY\t=\t{C}\n') == {C}
    assert wg.public_keys(f'[Peer]\nPublicKeу={C}\n') == set()


def test_drop_peers_byte_for_byte():
    text = HEADER + f'\n# first\n[Peer]\nPublicKey = {A}\nAllowedIPs = 192.0.2.2/32\n' + \
        f'\n# second \xe9\n[Peer]\nPublicKey = {B}\nAllowedIPs = 192.0.2.3/32\n# tail\n'
    assert wg.drop_peers(text, {A}) == HEADER + f'\n# second \xe9\n[Peer]\nPublicKey = {B}\nAllowedIPs = 192.0.2.3/32\n# tail\n'
    assert wg.drop_peers(text, {B}) == HEADER + f'\n# first\n[Peer]\nPublicKey = {A}\nAllowedIPs = 192.0.2.2/32\n\n# tail\n'
    assert wg.drop_peers(text, {A, B}) == HEADER + '\n# tail\n'
    assert wg.drop_peers(text, {C}) == text and wg.drop_peers(text, set()) == text
    assert wg.drop_peers(text, {A}) == wg.drop_peers(text, {A})


def test_drop_peers_empty_and_odd_input():
    assert wg.drop_peers('', {A}) == ''
    assert wg.drop_peers('no newline', {A}) == 'no newline'
    text = f'[Peer]\r\nPublicKey = {A}\r\n'
    assert wg.drop_peers(text, {A}) == ''
    # [Interface] с PublicKey не пир: остаётся
    keep = f'[Interface]\nPublicKey = {A}\n'
    assert wg.drop_peers(keep, {A}) == keep
    # NUL и регистр секции, ключ со вставкой NUL
    nul = f'[PEER]\x00\nPublic\x00Key = {A}\n'
    assert wg.drop_peers(nul, {A}) == ''


def test_addresses_wg_quick_rules():
    text = HEADER + 'Address = fd00::1/64 ,  198.51.100.1/24\n' + peer(A, 'Address = 203.0.113.1/32\n')
    assert wg.addresses(text) == ['192.0.2.1/24', 'fd00::1/64', '198.51.100.1/24']
    assert wg.addresses(text) == wg.addresses(text)
    assert wg.addresses('') == [] and wg.addresses('[Interface]\nAddress\n') == []
    assert wg.addresses('[INTERFACE]\n\tADDRESS=1.2.3.4/32 # c\nAddress=\n') == ['1.2.3.4/32']
    # нет секции: wg-quick Address не берёт
    assert wg.addresses('Address = 1.2.3.4/32\n') == []
    assert wg.addresses('[Interface]\nAddrеss = 1.2.3.4/32\n') == []


WGCF = f'''[Interface]
PrivateKey = {S}
Address = 172.16.0.2/32
Address = 2001:db8::2/128
DNS = 1.1.1.1
MTU = 1280
[Peer]
PublicKey = {A}
AllowedIPs = 0.0.0.0/0
Endpoint = engage.example.net:2408
'''


def test_profile_wgcf():
    assert wg.profile(WGCF) == {'private_key': S, 'address': '172.16.0.2', 'public_key': A,
                                'endpoint': ('engage.example.net', '2408')}
    assert wg.profile(WGCF) == wg.profile(WGCF)


def test_profile_forms_old_awk_broke_on():
    text = WGCF.replace(' = ', '=').replace('PrivateKey', 'privatekey').replace('\n', '\r\n')
    assert wg.profile(text) == wg.profile(WGCF)
    one_line = '[Interface]\nAddress = 2001:db8::2/128, 172.16.0.2/32\n[Peer]\nEndpoint = [2001:db8::1]:2408 # c\n'
    assert wg.profile(one_line) == {'private_key': None, 'address': '172.16.0.2', 'public_key': None,
                                    'endpoint': ('[2001:db8::1]', '2408')}


def test_profile_empty_and_broken():
    empty = {'private_key': None, 'address': None, 'public_key': None, 'endpoint': None}
    assert wg.profile('') == empty
    assert wg.profile('PrivateKey\nAddress 1.2.3.4\n# Endpoint = a:1\n') == empty
    assert wg.profile('Address = 2001:db8::2/128\n')['address'] is None
    assert wg.profile('Endpoint = nohost\n')['endpoint'] == ('', 'nohost')
    assert wg.profile('Эндпоинт = а:1\n') == empty
