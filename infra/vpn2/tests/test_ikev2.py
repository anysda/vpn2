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
        if argv[0] == 'le-sync':
            # Раскладка пары Caddy, как у anysda-ikev2-le-sync.sh: leaf, ключ и цепочка.
            for name, body in (('x509/anysda-server.crt', 'le leaf'), ('private/anysda-server.key', 'le key'),
                               ('x509ca/anysda-le-chain-1.crt', 'le chain')):
                (self.root / 'swanctl' / name).write_text(body)
        return subprocess.CompletedProcess(argv, code)

    def server(self, public=DOMAIN, panel=DOMAIN, restart=lambda: False):
        root = self.root
        self.said = []
        return ikev2.server('192.0.2.1', public, panel, '10.68.68.0/24', '10.99.0.1', self.said.append,
                            restart, etc=root / 'etc', swanctl=root / 'swanctl', target=root / 'pki',
                            caddy=root / 'caddy', lock_path=root / 'lock', le_sync='le-sync',
                            run=self.run, sleep=lambda _seconds: None)

    def bare(self):
        return self.server(public='192.0.2.1', panel='')

    def mark(self, name):
        return (self.root / 'etc' / name).read_text()

    def marks(self):
        return self.mark('ikev2-mode'), self.mark('ikev2-server-host'), self.mark('ikev2-domain')

    def conf(self):
        return (self.root / 'swanctl/conf.d/anysda.conf').read_text()

    def creds(self):
        return sorted(str(path.relative_to(self.root / 'swanctl')) for name in ('x509ca', 'x509', 'private')
                      for path in (self.root / 'swanctl' / name).iterdir())

    def issue(self):
        folder = self.root / 'caddy' / DOMAIN
        folder.mkdir(parents=True, exist_ok=True)
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


def test_fresh_domain_install_with_issued_certificate_serves_letsencrypt_only(node):
    folder = node.issue()
    assert node.server() is True
    assert node.calls == [['le-sync', f'{folder}/{DOMAIN}.crt', f'{folder}/{DOMAIN}.key'], *LOADS]
    assert node.creds() == ['private/anysda-server.key', 'x509/anysda-server.crt', 'x509ca/anysda-le-chain-1.crt']
    assert not (node.root / 'pki/ca.crt').exists()
    assert node.marks() == ('letsencrypt', DOMAIN, DOMAIN)
    assert f'      certs = anysda-server.crt\n      id    = {DOMAIN}\n' in node.conf()
    assert 'anysda-ikev2-ip' not in node.conf()
    assert node.server() is False


BOTH = ['private/anysda-own.key', 'private/anysda-server.key', 'x509/anysda-own.crt', 'x509/anysda-server.crt',
        'x509ca/anysda-ca.crt', 'x509ca/anysda-le-chain-1.crt']
BARE = ['private/anysda-server.key', 'x509/anysda-server.crt', 'x509ca/anysda-ca.crt']


def served_both(node):
    """Сервер на Let's Encrypt, а профили своего CA (по IP и по домену) остаются рабочими."""
    conf, swanctl = node.conf(), node.root / 'swanctl'
    assert node.marks() == ('letsencrypt', DOMAIN, DOMAIN)
    assert node.creds() == BOTH
    assert (swanctl / 'x509/anysda-server.crt').read_text() == 'le leaf'
    # Свой сертификат называет и домен, и IP входа; CA тот же, что в выданных профилях.
    assert (swanctl / 'x509/anysda-own.crt').read_bytes() == (node.root / 'pki/server.crt').read_bytes()
    assert (swanctl / 'private/anysda-own.key').read_bytes() == (node.root / 'pki/server.key').read_bytes()
    assert (swanctl / 'private/anysda-own.key').stat().st_mode & 0o777 == 0o600
    assert (swanctl / 'x509ca/anysda-ca.crt').read_bytes() == (node.root / 'pki/ca.crt').read_bytes()
    assert '      certs = anysda-server.crt, anysda-own.crt\n      id    = %any\n' in conf
    assert ('  anysda-ikev2-ip : connections.anysda-ikev2 {\n    local-server {\n'
            '      certs = anysda-own.crt\n      id    = 192.0.2.1\n') in conf
    assert not (node.root / 'etc/ikev2-own-ca-kept').exists()


@NEEDS_PKI
def test_domain_added_to_bare_ip_install_switches_to_letsencrypt_and_back(node):
    assert node.bare() is True
    bare_conf, ca = node.conf(), (node.root / 'pki/ca.crt').read_bytes()
    assert node.creds() == BARE
    folder = node.issue()
    node.calls.clear()
    assert node.server() is True
    assert node.calls == [['le-sync', f'{folder}/{DOMAIN}.crt', f'{folder}/{DOMAIN}.key'], *LOADS]
    served_both(node)
    assert (node.root / 'pki/ca.crt').read_bytes() == ca, 'профили до домена доверяют прежнему CA'
    assert any('свой CA' in line for line in node.said)
    assert node.server() is False
    # Обратно на голый IP: цепочка LE и второй комплект убраны и выгружены из charon.
    node.calls.clear()
    assert node.bare() is True
    assert node.calls == [['swanctl', '--load-creds', '--clear'], *LOADS[1:]]
    assert node.creds() == BARE
    assert node.conf() == bare_conf
    assert node.marks() == ('self-signed', '192.0.2.1', '')
    assert (node.root / 'swanctl/x509/anysda-server.crt').read_bytes() == (node.root / 'pki/server.crt').read_bytes()
    node.calls.clear()
    assert node.bare() is False
    assert node.calls == LOADS
    # И снова домен.
    assert node.server() is True
    served_both(node)
    assert (node.root / 'pki/ca.crt').read_bytes() == ca
    assert node.server() is False


@NEEDS_PKI
def test_install_kept_on_own_ca_by_old_marker_switches_to_letsencrypt(node):
    # Прежние версии оставляли такой сервер на своём CA навсегда отметкой ikev2-own-ca-kept.
    node.bare()
    (node.root / 'etc/ikev2-own-ca-kept').touch()
    node.server()
    assert node.marks() == ('self-signed', DOMAIN, DOMAIN)
    (node.root / 'etc/ikev2-own-ca-kept').touch()
    node.issue()
    assert node.server() is True
    served_both(node)


@NEEDS_PKI
def test_domain_without_certificate_stays_on_own_ca_until_caddy_issues(node):
    node.bare()
    assert node.server() is True
    assert node.marks() == ('self-signed', DOMAIN, DOMAIN)
    assert any('Caddy ещё не выдал' in line for line in node.said)
    # Раскладка та же, что будет после выдачи: слежению останется заменить пару сервера.
    pending = node.conf()
    assert '      certs = anysda-server.crt, anysda-own.crt\n      id    = %any\n' in pending
    assert node.creds() == [name for name in BOTH if 'le-chain' not in name]
    assert (node.root / 'swanctl/x509/anysda-server.crt').read_bytes() == (node.root / 'pki/server.crt').read_bytes()
    assert node.server() is False
    node.issue()
    assert node.server() is True
    served_both(node)
    assert node.conf() == pending


@NEEDS_PKI
def test_pending_domain_drops_chain_of_previous_certificate(node):
    node.bare()
    node.issue()
    node.server()
    node.bare()
    (node.root / 'swanctl/x509ca/anysda-le-chain-2.crt').write_text('stale')
    (node.root / 'caddy' / DOMAIN / f'{DOMAIN}.crt').unlink()
    node.server()
    assert node.marks() == ('self-signed', DOMAIN, DOMAIN)
    assert not list((node.root / 'swanctl/x509ca').glob('anysda-le-chain-*'))


@NEEDS_PKI
def test_bare_ip_state_restored_onto_domain_install_serves_both(node):
    # Restore вернул из архива голого IP свой CA, пару сервера и отметки режима.
    node.bare()
    node.issue()
    assert node.marks() == ('self-signed', '192.0.2.1', '')
    assert node.server() is True
    served_both(node)


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
