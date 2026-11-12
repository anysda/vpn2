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


def test_private_key_last_wins_and_blank_forms():
    text = f'[Interface]\nPrivateKey={A}\n  privatekey\t=\t{B} # c\n'
    assert wg.private_key(text) == B
    assert wg.private_key('') is None
    assert wg.private_key('# PrivateKey = x\n') is None
    # NUL внутри имени и пробелы внутри ключа: wg их не видит
    assert wg.private_key(f'[Interface]\nPriv\x00ateKey = {A[:10]} {A[10:]}\n') == A
    # пустое значение сбрасывает ключ, как в awk
    reset = f'[Interface]\nPrivateKey = {A}\nPrivateKey =\n'
    assert wg.private_key(reset) is None


def test_private_key_other_section_between_interfaces():
    text = f'[Interface]\nPrivateKey={A}\n[Peer]\nPrivateKey={B}\n[Interface]\nListenPort=1\n'
    assert wg.private_key(text) == A
    assert wg.private_key(f'[Interface] x\nPrivateKey={A}\n') is None


def test_addresses_wg_quick_rules():
    text = HEADER + 'Address = fd00::1/64 ,  198.51.100.1/24\n' + peer(A, 'Address = 203.0.113.1/32\n')
    assert wg.addresses(text) == ['192.0.2.1/24', 'fd00::1/64', '198.51.100.1/24']
    assert wg.addresses(text) == wg.addresses(text)
    assert wg.addresses('') == [] and wg.addresses('[Interface]\nAddress\n') == []
    assert wg.addresses('[INTERFACE]\n\tADDRESS=1.2.3.4/32 # c\nAddress=\n') == ['1.2.3.4/32']
    # нет секции: wg-quick Address не берёт
    assert wg.addresses('Address = 1.2.3.4/32\n') == []
    assert wg.addresses('[Interface]\nAddrеss = 1.2.3.4/32\n') == []
