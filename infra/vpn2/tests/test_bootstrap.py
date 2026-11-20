"""./deploy.sh на чистой системе: пара IP мастеру, службы менеджера и мастера, ссылка в терминал."""
import inspect
import subprocess
import time

import pytest

from vpn2 import tls, wizard
from vpn2.deploy import bootstrap

IP = '192.0.2.1'


def pair(folder, host):
    cert, key = folder / f'{host}.crt', folder / f'{host}.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-days', '30',
                    '-subj', f'/CN={host}', '-addext', f'subjectAltName={"IP" if host[0].isdigit() else "DNS"}:{host}',
                    '-keyout', key, '-out', cert], check=True, capture_output=True)
    return cert, key


@pytest.fixture
def caddy(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, 'CADDY', tmp_path)
    calls = []
    monkeypatch.setattr(tls, 'install_caddy', lambda: calls.append('install'))
    monkeypatch.setattr(tls, 'ip_certificate', lambda host, **kw: calls.append(('cert', host, kw)) or 'AB:CD')
    real = inspect.signature(tls.serve_routes)
    monkeypatch.setattr(tls, 'serve_routes', lambda *a, **kw: calls.append(('routes', dict(real.bind(*a, **kw).arguments))))
    return tmp_path, calls


def test_wizard_keeps_a_copy_of_the_ip_pair_and_not_a_domain_one(caddy, monkeypatch):
    folder, calls = caddy
    monkeypatch.setenv('TLS_ACME_URL', 'https://198.18.0.1:14000/dir')
    for host, kept in ((IP, IP), ('vpn.example.test', IP)):
        cert, key = pair(folder, host)
        cert.replace(folder / tls.SITE_CERT)
        key.replace(folder / tls.SITE_KEY)
        assert bootstrap.certificate(IP) == 'AB:CD'
        copied = tls.certificate(folder / 'vpn2-wizard.crt', folder / 'vpn2-wizard.key', kept, time.time())
        assert copied, f'после пары {host} у мастера не пара IP'
    assert calls[1] == ('cert', IP, {'acme_url': 'https://198.18.0.1:14000/dir', 'acme_root': ''})
    routes, site = wizard.site(IP)
    assert calls[2] == ('routes', {'name': 'wizard', 'routes': routes, 'site': site})
    assert '@wizard not path /api/version' in routes and f'https://{IP} {{\n    tls {folder}/vpn2-wizard.crt' in site
    assert f'reverse_proxy 127.0.0.1:{wizard.PORT}' in routes and 'import to_panel' in site


def test_units_run_from_the_tree_and_leave_the_venv_manager_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, 'UNITS', tmp_path)
    monkeypatch.setattr(bootstrap, 'NEEDRESTART', tmp_path / 'conf.d')
    (tmp_path / 'conf.d').mkdir()
    calls = []
    monkeypatch.setattr(bootstrap.subprocess, 'run', lambda argv, **_: calls.append(argv))
    bootstrap.units({'VPN2_DEVELOPMENT': 'true', 'TLS_ACME_URL': 'https://198.18.0.1:14000/dir'})
    manager, own = (tmp_path / 'vpn2-manager.service').read_text(), (tmp_path / 'vpn2-wizard.service').read_text()
    assert 'ExecStart=/usr/bin/python3 -B -m vpn2.manager serve' in manager and '-m vpn2.wizard serve' in own
    assert 'Environment="TLS_ACME_URL=https://198.18.0.1:14000/dir"\nEnvironment="VPN2_DEVELOPMENT=true"\n' in manager
    assert ['systemctl', 'enable', '--now', 'vpn2-manager.service', 'vpn2-wizard.service'] in calls
    # apt стадий с NEEDRESTART_MODE=a (openssh до сторожа фаервола) не перезапускает задание установки.
    assert (tmp_path / 'conf.d/vpn2.conf').read_text() == '$nrconf{override_rc}{qr(^vpn2-(manager|wizard))} = 0;\n'
    # Повтор после 29-control: служба менеджера уже из venv - не трогается.
    (tmp_path / 'vpn2-manager.service').write_text('venv unit\n')
    bootstrap.units({})
    assert (tmp_path / 'vpn2-manager.service').read_text() == 'venv unit\n'


@pytest.mark.parametrize('fingerprint', ['', 'AB:CD'])
def test_link_names_the_hour_and_the_fingerprint_when_self_signed(fingerprint, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(wizard, 'RUN', tmp_path)
    bootstrap.link(IP, fingerprint)
    out = capsys.readouterr().out
    assert f'https://{IP}/#' in out and 'Дальше в браузере; ссылка действует час, новая - ./deploy.sh' in out
    assert ('сертификат самоподписанный, отпечаток SHA-256 AB:CD' in out) == bool(fingerprint)
    assert ('браузер предупредит - сверьте отпечаток' in out) == bool(fingerprint)
