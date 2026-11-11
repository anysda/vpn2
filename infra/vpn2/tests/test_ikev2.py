"""Шаг сервера роли ikev2: порядок действий и отметки сверены со стадией 27-ikev2.sh."""
import shutil
import subprocess

import pytest

from vpn2 import ikev2
from vpn2.cli import host

DOMAIN = 'vpn.example.test'
NEEDS_PKI = pytest.mark.skipif(not shutil.which('pki'), reason='нет strongswan-pki')


class Node:
    """Каталоги узла во временной папке и журнал внешних вызовов."""

    def __init__(self, root):
        self.root, self.calls, self.fail = root, [], None
        (root / 'etc').mkdir()

    def run(self, argv, **kwargs):
        self.calls.append([str(part) for part in argv])
        code = 1 if self.fail in argv else 0
        if code and kwargs.get('check'):
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code)

    def server(self, public=DOMAIN, panel=DOMAIN, restart=lambda: False):
        root = self.root
        return ikev2.server('192.0.2.1', public, panel, '10.68.68.0/24', '10.99.0.1', lambda _text: None,
                            restart, etc=root / 'etc', swanctl=root / 'swanctl', target=root / 'pki',
                            caddy=root / 'caddy', lock_path=root / 'lock', le_sync='le-sync',
                            run=self.run, sleep=lambda _seconds: None)

    def mark(self, name):
        return (self.root / 'etc' / name).read_text()

    def issue(self):
        folder = self.root / 'caddy' / DOMAIN
        folder.mkdir(parents=True)
        for suffix in ('crt', 'key'):
            (folder / f'{DOMAIN}.{suffix}').write_text('pem')
        return folder


@pytest.fixture
def node(tmp_path):
    return Node(tmp_path)


LOADS = [['swanctl', what] for what in ('--load-creds', '--load-pools', '--load-conns')]


def test_conf_carries_server_id_and_pools():
    text = ikev2.conf_text('%any', '10.68.68.0/24', '10.99.0.1')
    assert '      id    = %any\n' in text
    assert '    addrs = 10.68.68.0/24\n    dns   = 10.99.0.1\n' in text
    assert f'    addrs = {ikev2.SUBNET6}\n' in text
    assert '$' not in text


@NEEDS_PKI
def test_own_ca_server_publishes_marks_after_load(node):
    assert node.server(public='192.0.2.1', panel='') is True
    assert node.calls == LOADS
    assert (node.mark('ikev2-mode'), node.mark('ikev2-server-host'), node.mark('ikev2-domain')) == (
        'self-signed', '192.0.2.1', '')
    swanctl = node.root / 'swanctl'
    assert (swanctl / 'private/anysda-server.key').stat().st_mode & 0o777 == 0o600
    assert (swanctl / 'private').stat().st_mode & 0o777 == 0o700
    assert '      id    = 192.0.2.1\n' in (swanctl / 'conf.d/anysda.conf').read_text()
    node.calls.clear()
    assert node.server(public='192.0.2.1', panel='') is False
    assert node.calls == LOADS


@NEEDS_PKI
def test_restart_waits_for_vici_before_load(node):
    assert node.server(public='192.0.2.1', panel='', restart=lambda: True) is True
    assert node.calls == [['swanctl', '--stats'], *LOADS]
    node.calls.clear()
    assert node.server(public='192.0.2.1', panel='', restart=lambda: True) is True


@NEEDS_PKI
def test_failed_load_keeps_previous_marks(node):
    node.server(public='192.0.2.1', panel='')
    node.fail = '--load-conns'
    with pytest.raises(subprocess.CalledProcessError):
        node.server()
    assert (node.mark('ikev2-mode'), node.mark('ikev2-server-host')) == ('self-signed', '192.0.2.1')


def test_letsencrypt_hands_caddy_pair_to_le_sync_and_drops_own_ca(node):
    folder = node.issue()
    (node.root / 'swanctl/x509ca').mkdir(parents=True)
    (node.root / 'swanctl/x509ca/anysda-ca.crt').write_text('own')
    assert node.server() is True
    assert node.calls == [['le-sync', f'{folder}/{DOMAIN}.crt', f'{folder}/{DOMAIN}.key'], *LOADS]
    assert not (node.root / 'swanctl/x509ca/anysda-ca.crt').exists()
    assert not (node.root / 'pki/ca.crt').exists()
    assert (node.mark('ikev2-mode'), node.mark('ikev2-server-host'), node.mark('ikev2-domain')) == (
        'letsencrypt', DOMAIN, DOMAIN)
    assert f'      id    = {DOMAIN}\n' in (node.root / 'swanctl/conf.d/anysda.conf').read_text()


def test_restored_letsencrypt_pair_is_not_touched_without_caddy(node):
    for name, body in (('etc/ikev2-mode', 'letsencrypt'), ('etc/ikev2-server-host', DOMAIN),
                       ('swanctl/x509/anysda-server.crt', 'restored'),
                       ('swanctl/private/anysda-server.key', 'restored')):
        (node.root / name).parent.mkdir(parents=True, exist_ok=True)
        (node.root / name).write_text(body)
    node.server()
    assert node.calls == LOADS
    assert (node.root / 'swanctl/x509/anysda-server.crt').read_text() == 'restored'
    assert node.mark('ikev2-mode') == 'letsencrypt'


def test_cli_step_reports_change(monkeypatch, capsys):
    seen = {}

    def server(*args):
        seen['args'] = args[:5]
        return True
    monkeypatch.setattr(ikev2, 'server', server)
    code = host.main(['ikev2-server', '--entry-host', '192.0.2.1', '--public-host', DOMAIN,
                      '--panel-domain', '', '--subnet', '10.68.68.0/24', '--dns', '10.99.0.1'])
    assert code == 0
    assert seen['args'] == ('192.0.2.1', DOMAIN, '', '10.68.68.0/24', '10.99.0.1')
    assert 'vpn2: changed' in capsys.readouterr().out
