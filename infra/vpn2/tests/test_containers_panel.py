"""Шаг панели: засев SSO, конфиг и пароль, перевод OpenVPN, решение не трогать контейнер."""

import subprocess
from pathlib import Path

import pytest
import yaml

from vpn2 import svc
from vpn2.adguard import stage as adguard
from vpn2.containers import compose, engine, panel

SSO = {'SSO_ENABLED': 'true', 'PANEL_DOMAIN': 'vpn.example.test',
       'SSO_DISCOVERY_URL': 'https://auth.example.test/.well-known/openid-configuration',
       'SSO_CLIENT_ID': 'vpn2', 'SSO_CLIENT_SECRET': 'test-only-secret', 'SSO_ALLOWED_SUBS': 'test-sub'}
OFF = {'sso_enabled': 'false', 'sso_auto_redirect': 'false', 'sso_password_login': 'true',
       'sso_redirect_url': ''}


def test_complete_seed_enables_sso():
    said = []
    facts = panel.sso({**SSO, 'SSO_PASSWORD_LOGIN': 'false'}, False, said.append)
    assert facts == {'sso_enabled': 'true', 'sso_auto_redirect': 'true', 'sso_password_login': 'false',
                     'sso_redirect_url': 'https://vpn.example.test/auth/authentik'}
    assert 'test-only-secret' not in ''.join(said)


@pytest.mark.parametrize('secret', ['', '  '])
def test_empty_secret_disables_sso_and_allows_password(secret):
    assert panel.sso({**SSO, 'SSO_CLIENT_SECRET': secret, 'SSO_PASSWORD_LOGIN': 'false'}, False) == OFF


@pytest.mark.parametrize('empty', ['SSO_DISCOVERY_URL', 'SSO_CLIENT_ID', 'SSO_ALLOWED_SUBS', 'PANEL_DOMAIN'])
def test_incomplete_seed_fails_first_install_but_not_panel_profile(empty):
    with pytest.raises(panel.StageError):
        panel.sso({**SSO, empty: ''}, False)
    # Настройки уже ведёт профиль панели: засев из config.yaml не проверяется.
    panel.sso({**SSO, empty: ''}, True)


@pytest.mark.parametrize('password', ['plain', 'p"a\\s$ {x}: #', "ква'рц\n2", '🔑' * 40])
def test_config_survives_any_password(tmp_path, password):
    template = 'auth:\n  user: ${ADMIN_USER_YAML}\n  password: ${ADMIN_PASS_YAML}\n'
    (tmp_path / 'tpl').write_text(template)
    owners = []
    target = tmp_path / 'config.yaml'
    environ = {'VPN2_PANEL_CONFIG_TPL': str(tmp_path / 'tpl')}
    assert panel.write_config(environ, 'adm in', password, target, lambda *args: owners.append(args[1:])) is True
    assert yaml.safe_load(target.read_text()) == {'auth': {'user': 'adm in', 'password': password}}
    assert target.stat().st_mode & 0o777 == 0o640 and owners == [('root', 'vpn2-panel')]
    assert panel.write_config(environ, 'adm in', password, target, lambda *_args: None) is False


def test_admin_password_from_config_else_kept_else_new(tmp_path):
    first, changed = panel.admin_password({}, tmp_path)
    assert changed and len(first) == 18 and first.isalnum()
    assert (tmp_path / 'admin-password.txt').stat().st_mode & 0o777 == 0o600
    assert panel.admin_password({}, tmp_path) == (first, False)
    assert panel.admin_password({'ADMIN_PASSWORD': 'из config'}, tmp_path) == ('из config', True)
    assert panel.admin_password({}, tmp_path) == ('из config', False)


@pytest.mark.parametrize('password', ['short-ASCII', 'a' * 72, 'a' * 73, 'я' * 36, 'я' * 37, '🔑' * 300])
def test_panel_logs_into_adguard_with_password_of_adguard_role(tmp_path, password):
    """Панель получает тот же пароль бэкенда, что роль adguard кладёт в хэш: длинный и UTF-8 тоже."""
    user, backend, _ = adguard.credentials({'ADMIN_PASSWORD': password}, tmp_path)
    assert panel.agh_password is adguard.agh_password
    assert adguard.hash_matches(user, panel.agh_password(password), adguard.new_hash(backend))


def test_session_secret_is_kept(tmp_path):
    value, created = panel.session_secret(tmp_path)
    assert created and len(value) == 64
    assert panel.session_secret(tmp_path) == (value, False)


def test_caddyfile_matches_bash_stage():
    """Эталон снят с heredoc стадии 30-frontend.sh до её удаления."""
    text = panel.caddyfile('192.0.2.1', 'https://192.0.2.1')
    assert text == (Path(__file__).parent / 'fixtures/containers/Caddyfile').read_text()
    assert 'default_sni 192.0.2.1\n' in text and '\nhttps://192.0.2.1 {\n' in text and '@' not in text.replace(
        '@internal', '')
    assert 'protocols h1 h2' in text and '/api/bot /api/bot/* /api/ops/bot-snapshot' in text
    assert 'header_up X-Forwarded-For {remote_host}' in text


class Apply:
    """vpn2-apply и systemctl: install кладёт кандидата в server.conf, как настоящий."""

    def __init__(self, conf, code=0, replace=True):
        self.conf, self.code, self.replace, self.calls = conf, code, replace, []

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv[1:3] if argv[0] == 'apply' else argv)
        if argv[:2] == ['apply', 'install']:
            if self.replace:
                self.conf.write_text(open(argv[3]).read())
            return subprocess.CompletedProcess(argv, self.code)
        return subprocess.CompletedProcess(argv, 0)


ROOT_CONF = 'port 1194\nmanagement-client-user root\nverb 3\n'
RESTART = ['systemctl', 'try-restart', 'openvpn-server@server']


@pytest.fixture
def openvpn(tmp_path, monkeypatch):
    conf = tmp_path / 'server.conf'
    conf.write_text(ROOT_CONF)
    monkeypatch.setattr(svc, 'APPLIED', str(tmp_path / 'applied'))
    monkeypatch.setattr(svc, 'fingerprint', lambda _paths: 'print')

    def make(**kwargs):
        apply = Apply(conf, **kwargs)
        monkeypatch.setattr(panel.subprocess, 'run', apply)
        return panel.Openvpn(print, conf, 'apply'), apply, conf

    return make


def test_management_user_switch_goes_through_apply_and_is_not_repeated(openvpn, tmp_path):
    unit, apply, conf = openvpn()
    assert unit.switch() is True
    assert conf.read_text() == ROOT_CONF.replace('root', 'vpn2-control')
    # Отпечаток записан: стадия 29 следующего деплоя не перезапустит OpenVPN ещё раз.
    assert (tmp_path / 'applied/openvpn-server@server.sha256').read_text() == 'print\n'
    apply.calls.clear()
    assert unit.switch() is False and apply.calls == []


def test_rollback_restarts_openvpn_only_after_switch(openvpn):
    unit, apply, _ = openvpn()
    unit.restored()
    assert RESTART not in apply.calls and ['systemctl', 'reload-or-restart', 'caddy'] in apply.calls
    unit.switch()
    unit.restored()
    assert RESTART in apply.calls and ['mark', 'openvpn'] in apply.calls


@pytest.mark.parametrize('replace', [True, False])
def test_failed_apply_fails_stage_and_rollback_follows_server_conf(openvpn, capsys, replace):
    """Код vpn2-apply не говорит, заменён ли файл: откат перезапускает OpenVPN по содержимому."""
    unit, apply, conf = openvpn(code=3, replace=replace)
    with pytest.raises(SystemExit) as failure:
        unit.switch()
    assert failure.value.code == 3 and unit.switched is replace
    assert ('уже заменён' if replace else 'не изменён') in capsys.readouterr().err
    unit.restored()
    assert (RESTART in apply.calls) is replace


def running(spec, running=True, project=compose.PROJECT):
    return {'State': {'Running': running}, 'Config': {'Labels': {
        compose.SPEC_LABEL: spec, engine.PROJECT_LABEL: project}}}


class Inspect:
    def __init__(self, container, logs=''):
        self.container, self.text, self.asked = container, logs, []

    def inspect(self, _name):
        return self.container

    def logs(self, _name, tail=None):
        self.asked.append(tail)
        return self.text

    def call(self, *_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, 'Exited (1)', '')


@pytest.mark.parametrize('container, health, kept', [
    (running('spec'), True, True),
    (running('other'), True, False),
    (running('spec', running=False), True, False),
    (running('spec'), False, False),
    (running('spec', project=None), True, False),
    (None, True, False),
])
def test_panel_is_kept_only_when_same_spec_runs_and_answers(monkeypatch, container, health, kept):
    monkeypatch.setattr(engine, 'http_ok', lambda _url: health)
    assert panel._kept(Inspect(container), 'spec') is kept


def test_broken_config_is_named_before_generic_failure():
    said = []
    docker = Inspect(None, 'boot\nanysda config /etc/anysda/config.yaml is invalid: bad\npanel init failed\n')
    with pytest.raises(panel.StageError):
        panel._diagnose(docker, said.append, False, False)
    assert 'не разобрала' in said[0] and 'is invalid: bad' in said[1] and len(said) == 2


@pytest.mark.parametrize('logs, http_up', [('', False), ('panel init failed', True)])
def test_dead_panel_fails_with_log_tail(logs, http_up):
    said = []
    docker = Inspect(None, logs)
    with pytest.raises(panel.StageError):
        panel._diagnose(docker, said.append, http_up, False)
    assert 'панель не поднялась' in said[0] and 'Exited (1)' in said[1] and docker.asked[-1] == 30
    # Отрицательный контроль: живая панель без ошибок init проходит, оставленную журнал не читают.
    panel._diagnose(Inspect(None, 'ok'), said.append, True, False)
    kept = Inspect(None, 'panel init failed')
    panel._diagnose(kept, said.append, True, True)
    assert kept.asked == []


@pytest.fixture
def caddy_host(tmp_path, monkeypatch):
    """Caddy узла: вставки в каталоге теста, systemctl подменён. Вернуть вставку панели и перезапуски."""
    from vpn2 import firewall
    files = [tmp_path / name for name in ('Caddyfile', 'vpn2-tls.conf', 'vpn2-http.conf', 'vpn2-panel.conf')]
    for item in files:
        item.write_text(item.name)
    restarts = []

    def run(argv, **_kwargs):
        if argv[:2] == ['systemctl', 'restart']:
            restarts.append(argv[2])
        return subprocess.CompletedProcess(argv, 0)
    restart, forget, applied = svc.restart_if_changed, svc.forget, tmp_path / 'applied'
    monkeypatch.setattr(panel, 'CADDY', [str(item) for item in files])
    monkeypatch.setattr(panel.subprocess, 'run', run)
    monkeypatch.setattr(firewall, 'ufw_reconcile', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(svc, 'restart_if_changed', lambda unit, paths: restart(unit, paths, applied))
    monkeypatch.setattr(svc, 'forget', lambda unit: forget(unit, applied))
    return files, restarts


def test_gate_opened_by_certificate_does_not_restart_caddy(caddy_host):
    # Вставку панели ведёт vpn2_tls.py: открыл панель после выпуска сертификата и сам
    # перечитал Caddy. Следующий деплой видел «новый» файл, перезапускал Caddy и
    # отчитывался changed=1 при нетронутом узле.
    files, restarts = caddy_host
    assert panel.caddy() is True
    files[3].write_text('import to_panel\n')
    assert panel.caddy() is False
    assert restarts == ['caddy']
    # Отрицательный контроль: вход, который пишет деплой, перезапускает по-прежнему.
    files[0].write_text('другой Caddyfile')
    assert panel.caddy() is True
    assert restarts == ['caddy', 'caddy']


def test_gate_rewritten_by_deploy_restarts_caddy(caddy_host):
    # Смена узла или пропавшая вставка: подготовка TLS закрыла панель сама, Caddy об этом не знает.
    _files, restarts = caddy_host
    panel.caddy()
    assert panel.caddy(regated=True) is True
    assert restarts == ['caddy', 'caddy']
