"""vpn2-apply остаётся одним файлом со своим разбором wg0.conf; служба читает тот же файл через vpn2.wg.
Обе стороны обязаны понимать строку одинаково."""
import pytest

from vpn2 import wg
from vpn2.control import apply

LINES = ['[Interface]', ' [ interface ] # c', '[Peer]', '[peer]#x', 'PrivateKey = abc=', 'privatekey=abc=',
         'ListenPort\t=\t51820', 'Address = 10.66.66.1/24', 'PostUp = iptables -A x', '# PrivateKey = no',
         'PublicKey = k # tail', 'AllowedIPs = 10.66.66.2/32, fd66:66::2/128', 'PresharedKey=', '', '   ',
         'FwMark = 0x1', 'Endpoint = [::1]:1', 'PersistentKeepalive = 25', 'Table = off', 'junk', '[Other]']
CONFS = ['[Interface]\nPrivateKey = a\nListenPort = 1\n',
         '[Interface]\nPrivateKey = a\n\n[Peer]\nPublicKey = b\nAllowedIPs = 10.66.66.2/32\n',
         '[Interface]\nPrivateKey = a\n\n# name\n# second\n[Peer]\nPublicKey = b\n\n[Peer]\nPublicKey = c\n',
         '[Interface]\nPrivateKey = a\n# note\n\n\n[peer] # x\nPublicKey = b', '', '[Peer]\nPublicKey = b\n',
         '[Interface]\r\nPrivateKey = a\r\n\r\n[Peer]\r\nPublicKey = b\r\n']


@pytest.mark.parametrize('text', LINES)
def test_line(text):
    assert wg.line(text) == apply.wg_line(text)


@pytest.mark.parametrize('text', CONFS)
def test_split(text):
    assert wg.split(text) == apply.wg_split(text)


def test_sections():
    assert wg.WG_SECTIONS == apply.WG_SECTIONS
