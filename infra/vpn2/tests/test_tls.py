"""HTTPS на IP входа, окна обновления по сроку сертификата и оповещения об ошибках ACME (vpn2.tls)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from panel_support import SRC, caddy, panel_tls
from vpn2 import resources, tls
from vpn2.containers import compose

# Запись журнала Caddy о недоступном каталоге ACME.
REFUSED = {'level': 'error', 'logger': 'tls.obtain', 'msg': 'could not get certificate from issuer',
           'identifier': '192.0.2.1', 'issuer': '127.0.0.1:1-directory',
           'error': 'registering account [] with server: provisioning client: performing request: '
                    'Get "https://127.0.0.1:1/directory": dial tcp 127.0.0.1:1: connect: connection refused'}
FACTS = {'session': 's', 'clash': 'c', 'tgbot': 't', 'admin_user': 'admin', 'agh_password': 'p',
         'sso_enabled': 'false', 'sso_auto_redirect': 'false', 'sso_password_login': 'true', 'sso_redirect_url': ''}
SNIPPETS = ('etc/caddy/Caddyfile', 'etc/caddy/vpn2-tls.conf', 'etc/caddy/vpn2-site.crt', 'etc/caddy/vpn2-site.key',
            'etc/vpn2/tls/config.json', 'etc/vpn2/tls/panel/secure')
LEGACY = {'etc/caddy/vpn2-http.conf': 'redir https://192.0.2.1{uri} permanent\n',
          'etc/caddy/vpn2-panel.conf': 'import to_panel\n'}


def stage(directory, domain, fail=False):
    stubs = directory / 'bin'
    stubs.mkdir(exist_ok=True)
    (stubs / 'systemctl').write_text(f'#!/bin/sh\necho "systemctl $*" >> {directory}/systemctl.log\n')
    (stubs / 'systemctl').chmod(0o755)
    return panel_tls(directory, domain, dict(os.environ, PATH=f'{stubs}:{os.environ["PATH"]}'), fail)


def validate(caddyfile):
    result = subprocess.run([caddy(), 'validate', '--config', str(caddyfile)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]


def window(config):
    return float(config.split('renewal_window_ratio ')[1].split()[0])


def test_ip_without_domain_gets_https_site_with_shortlived_profile(tmp_path):
    result = stage(tmp_path, '')
    assert result.returncode == 0, result.stderr
    caddy_dir = tmp_path / 'etc/caddy'
    caddyfile = (caddy_dir / 'Caddyfile').read_text()
    policy = (caddy_dir / 'vpn2-tls.conf').read_text()
    # ACME - на сайте loopback; сайт панели без имени отдаёт выгруженную пару, чужое имя обрывает.
    assert f'https://192.0.2.1:8443 {{\n    bind 127.0.0.1\n    import {caddy_dir}/vpn2-tls.conf' in caddyfile
    assert (f'https://:443 {{\n    tls {caddy_dir}/vpn2-site.crt {caddy_dir}/vpn2-site.key\n'
            f'    import {caddy_dir}/vpn2-site.d/*.conf\n    @foreign not host 192.0.2.1\n    abort @foreign\n') in caddyfile
    assert ':80 {\n    redir https://192.0.2.1{uri} permanent' in caddyfile and 'vpn2-sites.d' not in caddyfile
    adapted = json.loads(subprocess.run([caddy(), 'adapt', '--config', str(caddy_dir / 'Caddyfile')],
                                        capture_output=True, text=True, check=True).stdout)
    for server in adapted['apps']['http']['servers'].values():
        assert server['automatic_https'] == {'disable_redirects': True, 'ignore_loaded_certificates': True}
    assert 'default_sni 192.0.2.1' in caddyfile and 'respond @internal 404' in caddyfile
    assert 'protocols h1 h2\n' in caddyfile and 'h3' not in caddyfile.split('servers {')[1].split('}')[0]
    assert 'profile shortlived' in policy and 'renewal_window_ratio 0.250000000' in policy
    assert 'dir https://acme-v02.api.letsencrypt.org/directory' in policy
    assert json.loads((tmp_path / 'etc/vpn2/tls/config.json').read_text())['output_dir'] == f'{tmp_path}/etc/vpn2/tls/ip'
    # Панель всегда за HTTPS: Secure и https-источник CSRF с первого старта, без перезапуска после выпуска.
    assert (tmp_path / 'etc/vpn2/tls/panel/secure').read_text() == 'true\n'
    # До сертификата CA сайт отдаёт хранимую самоподписанную пару.
    kept = tmp_path / 'etc/vpn2/tls/self-signed'
    assert (caddy_dir / 'vpn2-site.crt').read_text() == (kept / 'fullchain.pem').read_text()
    assert (caddy_dir / 'vpn2-site.key').read_text() == (kept / 'privkey.pem').read_text()
    validate(caddy_dir / 'Caddyfile')


def test_domain_window_is_seven_days_before_expiry(tmp_path):
    assert stage(tmp_path, 'vpn.example.test').returncode == 0
    policy = (tmp_path / 'etc/caddy/vpn2-tls.conf').read_text()
    caddyfile = (tmp_path / 'etc/caddy/Caddyfile').read_text()
    assert f'https://vpn.example.test:8443 {{\n    bind 127.0.0.1\n    import {tmp_path}/etc/caddy/vpn2-tls.conf' in caddyfile
    assert '@foreign not host vpn.example.test\n' in caddyfile and 'redir https://vpn.example.test{uri}' in caddyfile
    # Браузер по IP приходит без SNI: имя рукопожатия - IP entry, у него может быть свой сайт (мастер установки).
    assert 'default_sni 192.0.2.1\n' in caddyfile
    assert caddyfile.endswith(f'}}\nimport {tmp_path}/etc/caddy/vpn2-sites.d/*.conf\n')
    assert 'profile' not in policy
    assert window(policy) * 90 == pytest.approx(7, abs=1e-6)
    validate(tmp_path / 'etc/caddy/Caddyfile')


def test_failed_panel_release_restores_previous_tls_snippets(tmp_path):
    # Первая установка упала: ни одного нового файла не осталось.
    assert stage(tmp_path, '', fail=True).returncode != 0
    assert [name for name in SNIPPETS if (tmp_path / name).exists()] == []
    # Рабочий выпуск на IP, потом упавший выпуск с доменом возвращает файлы IP и перечитывает Caddy.
    assert stage(tmp_path, '').returncode == 0
    before = {name: (tmp_path / name).read_bytes() for name in SNIPPETS}
    assert stage(tmp_path, 'vpn.example.test', fail=True).returncode != 0
    assert {name: (tmp_path / name).read_bytes() for name in SNIPPETS} == before
    assert 'systemctl reload-or-restart caddy' in (tmp_path / 'systemctl.log').read_text()
    # Обновление с 1.2.0: его вставки панели упавший выпуск возвращает вместе с прежним Caddyfile, удачный снимает.
    for name, text in LEGACY.items():
        (tmp_path / name).write_text(text)
    assert stage(tmp_path, '', fail=True).returncode != 0
    assert {name: (tmp_path / name).read_text() for name in LEGACY} == LEGACY
    assert stage(tmp_path, '').returncode == 0
    assert [name for name in LEGACY if (tmp_path / name).exists()] == []


def test_panel_reads_secure_flag_from_file_on_every_start():
    service = compose.panel_service({'HOST_TAG': 'ru', 'ENTRY_HOST': '192.0.2.1', 'AGH_PORT': '3000'}, 'image',
                                    resources.calculate(2048, 8192, {}), FACTS, user='1:1', control_gid=2)
    assert 'NUXT_SESSION_COOKIE_SECURE=$(cat /run/vpn2-tls/secure)' in ' '.join(service['command'])
    assert '/etc/vpn2/tls/panel:/run/vpn2-tls:ro' in service['volumes']
    assert 'NUXT_SESSION_COOKIE_SECURE' not in service['environment']


@pytest.mark.parametrize('host, lifetime, remaining', [
    ('192.0.2.1', 160 * 3600, 40 * 3600), ('192.0.2.1', 7 * 86400, 2 * 86400),
    ('vpn.example.test', 90 * 86400, 7 * 86400), ('vpn.example.test', 45 * 86400, 7 * 86400)])
def test_renewal_windows_follow_real_certificate_lifetime(host, lifetime, remaining):
    config = tls.tls_config({'host': host}, lifetime)
    assert lifetime * window(config) == pytest.approx(remaining, abs=.01)
    assert ('profile shortlived' in config) == (host == '192.0.2.1')
    assert 'disable_tlsalpn_challenge' in config


def test_exit_role_is_acme_client_on_port_80_and_leaves_443_free(tmp_path):
    subprocess.run([sys.executable, '-m', 'vpn2.cli', 'tls', '--prepare', '--role', 'exit',
                    '--config', f'{tmp_path}/config.json', '--caddy-dir', str(tmp_path),
                    '--host', '198.51.100.7', '--cert-group', 'root'], check=True, timeout=30,
                   env={**os.environ, 'PYTHONPATH': str(SRC)})
    caddyfile = (tmp_path / 'Caddyfile').read_text()
    assert 'https://198.51.100.7:8443 {\n    bind 127.0.0.1' in caddyfile
    assert ':80 {' in caddyfile and 'protocols h1 h2\n' in caddyfile
    config = json.loads((tmp_path / 'config.json').read_text())
    assert config['output_dir'] == '/etc/vpn2/tls/ip'
    assert 'secure_file' not in config and 'bot_secret' not in config
    assert 'profile shortlived' in (tmp_path / 'vpn2-tls.conf').read_text()
    validate(tmp_path / 'Caddyfile')


def test_acme_failure_reaches_bot_queue_with_expiry_and_recovery(tmp_path):
    received = []

    class Bot(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - имя задаёт BaseHTTPRequestHandler
            if self.headers['X-Tgbot-Secret'] != 'TEST_ONLY_SECRET':
                self.send_response(403)
                self.end_headers()
                return
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True, 'event_id': len(received)}).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Bot)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (tmp_path / 'secret').write_text('TEST_ONLY_SECRET\n')
        config = {'host': '192.0.2.1', 'storage': f'{tmp_path}/storage', 'acme_url': 'https://127.0.0.1:1/directory',
                  'bot_secret': f'{tmp_path}/secret', 'notify_hours': 6,
                  'bot_url': f'http://127.0.0.1:{server.server_port}/event'}
        state = tmp_path / 'state.json'
        with patch.object(tls, 'journal', return_value=[REFUSED]):
            assert not tls.tick(config, state, initial=True)
            tls.tick(config, state)
    finally:
        server.shutdown()
        server.server_close()
    assert len(received) == 1, 'repeat inside notify_hours must be throttled'
    assert received[0]['type'] == 'system_alert' and received[0]['subject'] == 'TLS: ошибка выпуска/обновления'
    assert '192.0.2.1: ACME: connection refused; срок истечения: сертификата нет' in received[0]['detail']
    assert '127.0.0.1:1' not in json.dumps(received)
    assert json.loads(state.read_text())['pending'] == []


def test_failure_throttle_restart_and_recovery():
    config, state, cert = {'host': '192.0.2.1'}, {}, {'fingerprint': 'first', 'end': 900000}
    tls.observe(config, state, cert, 'ACME: connection refused', 100)
    state = json.loads(json.dumps(state))
    tls.observe(config, state, cert, 'ACME: connection refused', 200)
    assert len(state['pending']) == 1 and '1970-01-11 10:00:00 UTC' in state['pending'][0]['detail']
    tls.observe(config, state, cert, None, 300)
    assert state['failed'], 'old valid certificate is not recovery'
    tls.observe(config, state, cert, 'ACME: timeout', 21700)
    assert len(state['pending']) == 2
    cert = dict(cert, fingerprint='renewed')
    tls.observe(config, state, cert, None, 21701)
    assert state['pending'][-1]['subject'] == 'TLS: обновлено' and not state['failed']
    tls.observe(config, state, cert, None, 21702)
    assert len(state['pending']) == 3


def test_expiry_remains_in_alert_after_expiration():
    state = {}
    tls.observe({'host': 'vpn.example.test'}, state, {'fingerprint': 'first', 'end': 900000}, None, 100)
    tls.observe({'host': 'vpn.example.test'}, state, None, 'ACME: timeout', 900001)
    assert '1970-01-11 10:00:00 UTC' in state['pending'][0]['detail']


def test_error_redacts_secrets_and_ignores_other_sites():
    error = {'level': 'error', 'logger': 'tls.obtain', 'identifier': '192.0.2.1',
             'error': 'urn:ietf:params:acme:error:rejectedIdentifier secret=value https://user:pass@ca/'}
    assert tls.safe_error([error], '192.0.2.1') == 'ACME: rejectedIdentifier'
    assert tls.safe_error([error], '192.0.2.2') is None
    assert tls.safe_error([REFUSED], '192.0.2.1') == 'ACME: connection refused'


def test_rate_limit_names_http_code_and_retry_time():
    error = {'level': 'error', 'logger': 'tls.obtain', 'identifier': '192.0.2.1',
             'error': 'HTTP 429 urn:ietf:params:acme:error:rateLimited - too many certificates (5) already issued '
                      'for this exact set of identifiers in the last 168h0m0s, retry after 2026-11-16 00:02:29 UTC: '
                      'see https://letsencrypt.org/docs/rate-limits/'}
    assert (tls.safe_error([error], '192.0.2.1')
            == 'ACME: rateLimited (HTTP 429), повтор после 2026-11-16 00:02:29 UTC')


def test_first_failure_queue_survives_unavailable_bot():
    state = {}
    tls.observe({'host': '192.0.2.1'}, state, None, 'ACME: timeout', 100)
    tls.deliver({'bot_secret': '/nonexistent-test-bot-secret'}, state)
    tls.deliver({}, state)
    assert len(state['pending']) == 1
    tls.observe({'host': '192.0.2.1'}, state, {'fingerprint': 'first', 'end': 900000}, None, 200)
    assert [event['subject'] for event in state['pending']] == ['TLS: ошибка выпуска/обновления', 'TLS: обновлено']


def test_export_validates_san_key_switches_pair_and_prunes_old(tmp_path):
    items = []
    for index in range(3):
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-nodes', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                        '-days', '6', '-subj', '/CN=test', '-addext', 'subjectAltName=IP:192.0.2.1',
                        '-out', tmp_path / f'cert{index}.pem', '-keyout', tmp_path / f'key{index}.pem'],
                       check=True, capture_output=True)
        items.append(tls.certificate(tmp_path / f'cert{index}.pem', tmp_path / f'key{index}.pem', '192.0.2.1', time.time()))
    assert items[0] is not None
    assert tls.certificate(tmp_path / 'cert0.pem', tmp_path / 'key0.pem', '192.0.2.2', time.time()) is None
    assert tls.certificate(tmp_path / 'cert0.pem', tmp_path / 'key0.pem', '192.0.2.1', time.time() + 7 * 86400) is None
    assert tls.certificate(tmp_path / 'cert0.pem', tmp_path / 'key1.pem', '192.0.2.1', time.time()) is None
    config = {'output_dir': str(tmp_path / 'ip')}
    for item in items:
        tls.export_certificate(config, item)
    assert (tmp_path / 'ip/fullchain.pem').read_bytes() == (tmp_path / 'cert2.pem').read_bytes()
    assert (tmp_path / 'ip/privkey.pem').read_bytes() == (tmp_path / 'key2.pem').read_bytes()
    assert (tmp_path / 'ip/privkey.pem').stat().st_mode & 0o777 == 0o640
    generations = sorted(path.name for path in (tmp_path / 'ip').iterdir() if not path.is_symlink())
    assert generations == sorted([items[1]['fingerprint'], items[2]['fingerprint']])
    tls.export_certificate(config, items[2])
    assert os.readlink(tmp_path / 'ip/current') == items[2]['fingerprint']


def test_switch_is_a_file_swap_and_reload_never_a_panel_restart(tmp_path):
    caddy_dir = tmp_path / 'caddy'
    caddy_dir.mkdir()
    config = {'role': 'panel', 'host': '192.0.2.1', 'caddy_dir': str(caddy_dir), 'output_dir': str(tmp_path / 'tls/ip')}
    own = tls.self_signed(config)
    assert tls.self_signed(config) == own, 'пара хранится: отпечаток тот же от деплоя к деплою'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-nodes', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                    '-days', '6', '-subj', '/CN=ca', '-addext', 'subjectAltName=IP:192.0.2.1',
                    '-out', tmp_path / 'ca.pem', '-keyout', tmp_path / 'ca.key'], check=True, capture_output=True)
    ca = tls.certificate(tmp_path / 'ca.pem', tmp_path / 'ca.key', '192.0.2.1', time.time())
    assert tls.serve(config, None, reload=False) == own
    before = {path.name: path.read_text() for path in caddy_dir.iterdir()}
    with patch.object(tls.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'caddy')), \
            pytest.raises(subprocess.CalledProcessError):
        tls.serve(config, ca)
    assert {path.name: path.read_text() for path in caddy_dir.iterdir()} == before
    with patch.object(tls.subprocess, 'run') as run:
        assert tls.serve(config, ca) == ca
        assert [call.args[0][:2] for call in run.call_args_list] == [['caddy', 'validate'], ['systemctl', 'reload']]
        assert (caddy_dir / 'vpn2-site.crt').read_bytes() == (tmp_path / 'ca.pem').read_bytes()
        run.reset_mock()
        tls.serve(config, ca)
        run.assert_not_called()
        # Сертификат CA пропал (истёк): назад та же самоподписанная пара.
        assert tls.serve(config, None)['fingerprint'] == own['fingerprint']
        assert [call.args[0][:2] for call in run.call_args_list if call.args[0][0] != 'openssl'] == [
            ['caddy', 'validate'], ['systemctl', 'reload']]
    assert (caddy_dir / 'vpn2-site.crt').read_text() == Path(own['cert']).read_text()
    assert (caddy_dir / 'vpn2-site.key').stat().st_mode & 0o777 == 0o640
    assert tls.fingerprint(own['cert']) == subprocess.run(
        ['openssl', 'x509', '-in', own['cert'], '-noout', '-fingerprint', '-sha256'],
        capture_output=True, text=True, check=True).stdout.strip().split('=')[1]


def test_exit_serve_only_updates_window(tmp_path):
    config = {'host': '198.51.100.7', 'caddy_dir': str(tmp_path)}
    (tmp_path / 'vpn2-tls.conf').write_text(tls.tls_config(config))
    with patch.object(tls.subprocess, 'run') as run:
        assert tls.serve(config, {'start': 0, 'end': 7 * 86400}) is None
    assert [call.args[0][0] for call in run.call_args_list] == ['caddy', 'systemctl']
    assert sorted(os.listdir(tmp_path)) == ['vpn2-tls.conf']
    assert 'renewal_window_ratio 0.285714286' in (tmp_path / 'vpn2-tls.conf').read_text()


def test_initial_wait_ends_on_refusal_and_is_skipped_while_refusal_is_latched(tmp_path, capsys):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'role': 'exit', 'host': '192.0.2.1', 'storage': str(tmp_path / 'storage')}))
    log = []

    def journal(state):
        # Как journalctl --after-cursor; Caddy отказывает после первого взгляда в журнал.
        new, state['cursor'] = log[state.get('cursor', 0):], len(log)
        log[:] = log or [REFUSED]
        return new

    with patch.object(tls, 'journal', side_effect=journal), \
            patch.object(tls.time, 'sleep') as sleep:
        tls.main(['--initial', '--config', str(config), '--wait', '60'])
        assert sleep.call_count == 1, 'отказ в журнале Caddy обрывает ожидание'
        state = json.loads((tmp_path / 'state.json').read_text())
        assert state['failed'] and state['failed_host'] == '192.0.2.1'
        sleep.reset_mock()
        tls.main(['--initial', '--config', str(config), '--wait', '60'])
        sleep.assert_not_called()
        assert capsys.readouterr().out.count('ACME: connection refused') == 2
        config.write_text(json.dumps({'role': 'exit', 'host': '192.0.2.9', 'storage': str(tmp_path / 'storage')}))
        tls.main(['--initial', '--config', str(config), '--wait', '1'])
        assert sleep.called, 'новый хост ждёт первый выпуск'


def test_ip_certificate_prepares_the_ip_site_and_returns_the_self_signed_fingerprint(tmp_path):
    calls = []
    state = tmp_path / 'state.json'
    with patch.object(tls, 'main', side_effect=lambda argv: calls.append(argv[0])), \
            patch.object(tls.subprocess, 'run') as run, patch.object(tls, 'read_json',
                                                                    side_effect=lambda *_args: json.loads(state.read_text())):
        state.write_text(json.dumps({'self_signed': 'AB:CD'}))
        assert tls.ip_certificate('192.0.2.1', wait=5) == 'AB:CD'
        state.write_text('{}')
        assert tls.ip_certificate('192.0.2.1') == ''
    assert calls == ['--prepare', '--initial'] * 2
    assert run.call_args_list[0].args[0] == ['systemctl', 'reload-or-restart', 'caddy']
    with pytest.raises(ValueError):
        tls.serve_routes('../x', 'route {}')


def test_install_caddy_runs_the_pinned_installer_and_fails_loudly():
    from vpn2 import deploy
    with patch.object(deploy, 'svc', return_value=0) as svc:
        tls.install_caddy()
    svc.assert_called_once_with('caddy_install')
    with patch.object(deploy, 'svc', return_value=1), pytest.raises(RuntimeError):
        tls.install_caddy()
