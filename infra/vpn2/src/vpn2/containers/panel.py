"""Шаг стадии 30: панель на entry.

Панель слушает 127.0.0.1:51821 в сети хоста без capabilities, наружу её отдаёт Caddy.
Замена панели идёт транзакцией: неудача возвращает файлы хоста и прежний образ.
"""

import grp
import json
import os
import pwd
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from vpn2 import fsutil, svc
from vpn2.adguard.stage import admin_password, agh_password
from vpn2.containers import compose, engine
from vpn2.containers.transaction import DATA, HEALTH, Transaction

STAGE = '30-frontend'
ETC = '/etc/anysda'
CONFIG = f'{ETC}/anysda-config.yaml'
TEMPLATE = '/tmp/anysda/anysda-config.yaml.tpl'
SSO_RUNTIME = f'{ETC}/sso-runtime.json'
TLS = '/usr/local/lib/vpn2/vpn2_tls.py'
TLS_CONFIG = '/etc/vpn2/tls/config.json'
SECURE = '/etc/vpn2/tls/panel/secure'
MANAGED = '/etc/vpn2-control/api-managed'
CADDY = ['/etc/caddy/Caddyfile', '/etc/caddy/vpn2-tls.conf', '/etc/caddy/vpn2-http.conf',
         '/etc/caddy/vpn2-panel.conf']
OVPN_CONF = '/etc/openvpn/server/server.conf'
OVPN_USER = 'management-client-user vpn2-control'
OVPN_INPUTS = ['/usr/sbin/openvpn', '/usr/lib/systemd/system/openvpn-server@.service', OVPN_CONF,
               '/etc/openvpn/server/pki/ca.crt', '/etc/openvpn/server/pki/server.crt',
               '/etc/openvpn/server/pki/server.key', '/etc/openvpn/server/pki/tls-crypt.key']
APPLY = '/usr/local/sbin/vpn2-apply'


class StageError(Exception):
    pass


def _quiet(*argv):
    return subprocess.run(list(argv), check=False, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode


def db_owner(data=DATA, chown=shutil.chown):
    """Миграция идёт от root и на первой установке создаёт базу с владельцем root."""
    chown(data, 'vpn2-panel', 'vpn2-panel')
    for name in ('db.sqlite', 'db.sqlite-wal', 'db.sqlite-shm'):
        if os.path.lexists(os.path.join(data, name)):
            chown(os.path.join(data, name), 'vpn2-panel', 'vpn2-panel')


def sso(environ, runtime_exists, say=lambda _text: None):
    """Засев SSO из config.yaml: безопасен и после удаления настроек профиля панели."""
    get = environ.get
    enabled = get('SSO_ENABLED') or 'false'
    auto, password_login = get('SSO_AUTO_REDIRECT') or 'true', get('SSO_PASSWORD_LOGIN') or 'true'
    domain = get('PANEL_DOMAIN') or ''
    if not (get('SSO_CLIENT_SECRET') or '').strip():
        enabled = 'false'
    if enabled != 'true':
        auto, password_login = 'false', 'true'
    redirect = f'https://{domain}/auth/authentik' if enabled == 'true' and domain else ''
    if runtime_exists:
        say(f'  SSO: настроен в профиле панели ({SSO_RUNTIME}), блок sso: из config.yaml не применяется')
    elif enabled == 'true':
        # Адрес возврата строится от домена, куки state/PKCE только Secure: без домена
        # вход падал бы на возврате и выглядел бы поломкой Authentik.
        if not domain:
            raise StageError('✗ sso.enabled=true, но panel.domain не задан.\n'
                             '  SSO работает только по HTTPS: куки state/PKCE — Secure-only.')
        for name in ('SSO_DISCOVERY_URL', 'SSO_CLIENT_ID', 'SSO_ALLOWED_SUBS'):
            if not get(name):
                raise StageError(f'{name} пуст' + (' — войти не сможет никто' if name.endswith('SUBS') else ''))
        say(f'  SSO: {get("SSO_CLIENT_ID")} @ {get("SSO_DISCOVERY_URL")}')
        say(f'  SSO redirect_uri: {redirect} (должен совпадать с провайдером в Authentik)')
    else:
        say('  SSO: выключен')
    return {'sso_enabled': enabled, 'sso_auto_redirect': auto, 'sso_password_login': password_login,
            'sso_redirect_url': redirect}


def render_config(template, user, password):
    """JSON-строка = YAML-строка: кавычки и спецсимволы пароля не ломают конфиг."""
    def quote(value):
        return json.dumps(value, ensure_ascii=False)
    return template.replace('${ADMIN_USER_YAML}', quote(user)).replace('${ADMIN_PASS_YAML}', quote(password))


def write_config(environ, user, password, path=CONFIG, chown=shutil.chown):
    template = Path(environ.get('VPN2_PANEL_CONFIG_TPL') or TEMPLATE)
    if not template.is_file():
        raise StageError(f'{template} не найден')
    changed = fsutil.publish(path, render_config(template.read_text(), user, password), 0o640)
    chown(path, 'root', 'vpn2-panel')
    os.chmod(path, 0o640)
    return changed


def session_secret(etc=ETC):
    """Постоянный секрет сессий: куки переживают пересоздание контейнера."""
    path = Path(etc, 'session-secret.txt')
    changed = not path.is_file() and fsutil.publish(path, secrets.token_hex(32) + '\n', 0o600)
    return path.read_text().strip(), changed


def caddyfile(tls_host, site, root=''):
    return (CADDYFILE.replace('@TLS_HOST@', tls_host).replace('@SITE@', site)
            .replace('import /etc/caddy/', f'import {root}/etc/caddy/'))


CADDYFILE = '''{
    # :80 только переадресует на HTTPS (/etc/caddy/vpn2-http.conf), ACME
    # HTTP-01 Caddy отвечает до маршрутов. Панель открыта лишь при действующем
    # сертификате от CA: /etc/caddy/vpn2-panel.conf ведёт vpn2_tls.py.
    auto_https disable_redirects
    # Клиент по IP не шлёт SNI.
    default_sni @TLS_HOST@
    # HTTP/3 выключен намеренно: Caddy с h3 держит *:443/udp, а sing-box при
    # возврате UDP через tproxy биндит transparent-сокет на <адрес-цели>:443.
    # Порт занят -> «bind: address already in use», и QUIC-ответы не доходят
    # НИ ДО ОДНОГО клиента туннеля (VPN2-22). Панели h3 не нужен.
    servers {
        protocols h1 h2
    }
}
# Пути бота — только для самой ноды: бот ходит на 127.0.0.1:51821 мимо
# Caddy. Caddy сам подключается к
# панели с 127.0.0.1, поэтому проверка loopback в панели отсюда бесполезна:
# закрываем здесь (VPN2-51). Остальной /api/ops (nodes, adguard) зовёт
# браузер админа со страницы мониторинга — его не трогаем.
(internal_only) {
    @internal path /api/bot /api/bot/* /api/ops/bot-snapshot
    respond @internal 404
}
# X-Forwarded-For панели ставит только Caddy, перезаписывая присланный
# клиентом: по нему считается лимит попыток входа (VPN2-63). По умолчанию
# Caddy и так перезаписывает, но с trusted_proxies начал бы дописывать;
# его предупреждение «Unnecessary header_up» при запуске ожидаемо.
(to_panel) {
    reverse_proxy 127.0.0.1:51821 {
        header_up X-Forwarded-For {remote_host}
        # Node closes idle HTTP/1 sockets; never reuse one for a mutating POST.
        transport http {
            keepalive off
        }
    }
}
@SITE@ {
    import /etc/caddy/vpn2-tls.conf
    import internal_only
    encode gzip
    import /etc/caddy/vpn2-panel.conf
}
:80 {
    import /etc/caddy/vpn2-http.conf
}
'''


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


class Openvpn:
    """Перевод management-client-user на vpn2-control через vpn2-apply и его откат."""

    def __init__(self, say, conf=OVPN_CONF, apply=APPLY):
        self.say, self.conf, self.apply = say, Path(conf), apply
        self.switched = False

    def mark_restarted(self):
        """vpn2-apply уже перезапустил OpenVPN: стадия 29 не должна делать это второй раз."""
        target = Path(svc.APPLIED, 'openvpn-server@server.sha256')
        target.parent.mkdir(parents=True, exist_ok=True)
        fsutil.atomic(target, svc.fingerprint(OVPN_INPUTS) + '\n', mode=0o644)

    def switch(self):
        if not self.conf.is_file():
            return False
        lines = self.conf.read_text().splitlines(keepends=True)
        if OVPN_USER + '\n' in lines or OVPN_USER in lines:
            return False
        # Через vpn2-apply: last good получает тот же конфиг, и откат не вернёт root.
        candidate = ''.join(OVPN_USER + '\n' if line.startswith('management-client-user ') else line
                            for line in lines)
        with tempfile.NamedTemporaryFile('w', delete=False) as handle:
            handle.write(candidate)
        try:
            code = subprocess.run([self.apply, 'install', 'openvpn', handle.name], check=False).returncode
            # Прерванный apply мог уже заменить server.conf и перезапустить OpenVPN.
            # Код 1 бывает и без замены файла, поэтому решает содержимое, а не код.
            self.switched = self.conf.read_text() == candidate
        finally:
            os.unlink(handle.name)
        if code:
            state = 'уже заменён' if self.switched else 'не изменён'
            print(f'[{os.environ.get("HOST_TAG", "ru")}] vpn2-apply install openvpn: код {code}, '
                  f'server.conf {state}', file=sys.stderr)
            raise SystemExit(code)
        self.switched = True
        self.mark_restarted()
        return True

    def restored(self):
        """Файлы хоста возвращены: Caddy и OpenVPN перечитывают прежние."""
        subprocess.run(['systemctl', 'reload-or-restart', 'caddy'], check=False)
        if self.switched:
            subprocess.run(['systemctl', 'try-restart', 'openvpn-server@server'], check=False)
            if subprocess.run([self.apply, 'mark', 'openvpn'], check=False).returncode:
                print('vpn2-apply mark openvpn не прошёл: last good не совпадает с server.conf',
                      file=sys.stderr)
            self.mark_restarted()
        if os.path.isfile('/etc/systemd/system/vpn2-tls.timer'):
            subprocess.run(['systemctl', 'start', 'vpn2-tls.timer'], check=False)


def host_tls(environ, say, tx, root='', lib=TLS):
    """Вставки TLS и Caddyfile под охраной выпуска панели. Вернуть узел, сайт и менялся ли Caddyfile.

    root переносит /etc и /var в каталог теста.
    """
    domain = environ.get('PANEL_DOMAIN') or ''
    entry = environ['ENTRY_HOST']
    if domain:
        tls_host, site, out = domain, domain, '/etc/vpn2/tls/domain'
        say(f"  домен панели: {domain} (HTTPS через Let's Encrypt)")
    else:
        tls_host, site, out = entry, f'https://{entry}', '/etc/vpn2/tls/ip'
        say(f"  домен не задан — HTTPS на IP {entry} (Let's Encrypt, ~6 дней)")
    # Вставки TLS и server.conf принадлежат выпуску панели: откат возвращает их вместе с ней.
    tx.guard_files([root + path for path in (*CADDY, TLS_CONFIG, SECURE, MANAGED, OVPN_CONF)])
    subprocess.run(
        [sys.executable, lib, '--prepare', '--role', 'panel', '--config', root + TLS_CONFIG,
         '--caddy-dir', f'{root}/etc/caddy', '--storage', f'{root}/var/lib/caddy/.local/share/caddy',
         '--secure-file', root + SECURE, '--bot-secret', f'{root}{ETC}/tgbot-secret.txt',
         '--panel-container', compose.PANEL, '--host', tls_host, '--output-dir', root + out,
         '--acme-url', environ.get('TLS_ACME_URL') or 'https://acme-v02.api.letsencrypt.org/directory',
         '--acme-root', environ.get('TLS_ACME_ROOT') or ''], check=True)
    return tls_host, site, fsutil.publish(root + CADDY[0], caddyfile(tls_host, site, root), 0o644)


def caddy(regated=False):
    """Правила ufw и перезапуск Caddy при смене входов. Вернуть, перезапущен ли он.

    Вставку панели ведёт vpn2_tls.py и после каждой своей правки сам перечитывает
    Caddy: в отпечаток она не входит, иначе деплой после выпуска сертификата
    перезапускал бы Caddy зря. regated - вставку переписала подготовка TLS этого деплоя.
    """
    from vpn2 import firewall
    firewall.ufw_reconcile('caddy HTTP + ACME challenge', ['80/tcp'], ['caddy HTTP панель'])
    firewall.ufw_reconcile('caddy HTTPS панель', ['443/tcp'])
    # Веб-интерфейс AdGuard наружу не открыт; порт 3001 прошлых установок закрываем.
    _quiet('ufw', 'delete', 'allow', '3001/tcp')
    if _quiet('caddy', 'validate', '--config', CADDY[0]):
        raise StageError('конфиг caddy невалиден')
    if regated:
        svc.forget('caddy')
    return svc.restart_if_changed('caddy', ['/usr/bin/caddy', '/usr/lib/systemd/system/caddy.service',
                                            *CADDY[:3]])


def _kept(docker, spec):
    """Работающая панель создана из того же описания и отвечает: её не трогаем."""
    current = docker.inspect(compose.PANEL)
    if not current or not current['State'].get('Running'):
        return False
    labels = current['Config'].get('Labels') or {}
    return (labels.get(compose.SPEC_LABEL) == spec
            and labels.get(engine.PROJECT_LABEL) == compose.PROJECT
            and engine.http_ok(HEALTH))


def _diagnose(docker, say, http_up, kept):
    logs = '' if kept else docker.logs(compose.PANEL)
    # Битый anysda-config.yaml роняет init панели. Причину печатаем раньше общей
    # проверки: чинить надо конфиг, а не панель.
    bad = [line for line in logs.splitlines() if 'anysda config ' in line and ' is invalid' in line]
    if bad:
        say(f'✗ панель не разобрала {CONFIG}, пароль админа не применён:')
        say(f'  {bad[-1][:400]}')
        raise StageError('')
    if not http_up or 'panel init failed' in logs:
        say('✗ панель не поднялась: /api/version не ответил 2xx за 60 с или init упал')
        status = docker.call('ps', '-a', '--filter', f'name=^{compose.PANEL}$', '--format', '{{.Status}}',
                             check=False).stdout.strip()
        say(f'  контейнер: {status}')
        say('  хвост журнала панели:')
        for line in docker.logs(compose.PANEL, tail=30).splitlines():
            say(f'    {line}')
        raise StageError('')


def _summary(environ, say, admin_user, sso_enabled):
    domain = environ.get('PANEL_DOMAIN') or ''
    opened = any(line.startswith('import to_panel') for line in _read(CADDY[3]).splitlines())
    closed = '' if opened else ' (закрыта до выпуска сертификата)'
    secret = f'{admin_user} / (пароль в {ETC}/admin-password.txt)'
    print()
    say('-------------------------------------------------------------')
    say(f' Панель:        https://{domain or environ["ENTRY_HOST"]}/{closed}')
    if os.path.isfile(SSO_RUNTIME):
        say(' Вход:          как настроен в профиле панели; break-glass /login?direct=1')
        say(f'                {secret}')
    elif sso_enabled == 'true':
        say(' Вход:          через Authentik (бесшовно)')
        say(f' Break-glass:   https://{domain}/login?direct=1')
        say(f'                {secret}')
    else:
        say(f' Логин:         {secret}')
    say('-------------------------------------------------------------')


def _stage(environ, docker, say, tx, image, sleep):
    panel = pwd.getpwnam('vpn2-panel')
    control_gid = grp.getgrnam('vpn2-control').gr_gid
    user = f'{panel.pw_uid}:{panel.pw_gid}'
    # Код кандидата сам проверяет свои требования к API под настоящим UID контейнера.
    docker.call('run', '--rm', '--network', 'none', '--user', user, '--group-add', str(control_gid),
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '-v', '/run/vpn2-control:/run/vpn2-control:ro',
                '--entrypoint', 'node', image, 'scripts/control-probe.mjs', capture_output=False)
    tx.begin(image)
    db_owner()

    say('[2/4] config')
    password, changed = admin_password(environ)
    admin_user = environ.get('ADMIN_USER') or 'anysda'
    facts = sso(environ, os.path.isfile(SSO_RUNTIME), say)
    changed |= write_config(environ, admin_user, password)
    session, created = session_secret()
    changed |= created
    say(f'  admin user:  {admin_user}')
    say(f'  admin pass:  (см. {ETC}/admin-password.txt на ноде)')

    say('[3/4] caddy')
    subprocess.run(['systemctl', 'stop', 'vpn2-tls.timer', 'vpn2-tls.service'], check=False,
                   stderr=subprocess.DEVNULL)
    openvpn = Openvpn(say)
    tx.files_restored = openvpn.restored
    gate = _read(CADDY[3])
    _host, _site, published = host_tls(environ, say, tx)
    changed |= published
    changed |= caddy(regated=_read(CADDY[3]) != gate)

    say('[4/4] run container')
    engine.drop_timezone_dir()
    # Сеть хоста нужна для loopback AdGuard, метрик и HTTP бота; на систему панель
    # действует только через сокет vpn2-control.
    subprocess.run(['systemctl', 'stop', 'anysda-ikev2-sync.timer', 'anysda-ikev2-sync.service'],
                   check=False, stderr=subprocess.DEVNULL)
    changed |= openvpn.switch()
    managed = Path(MANAGED)
    changed |= not managed.exists()
    managed.touch()
    subprocess.run(['systemctl', 'start', 'anysda-ikev2-sync.timer'], check=False, stderr=subprocess.DEVNULL)

    facts.update(session=session, clash=_read(f'{ETC}/clash-secret.txt'),
                 tgbot=_read(f'{ETC}/tgbot-secret.txt'), admin_user=admin_user,
                 agh_password=agh_password(password))
    text = engine.resource_text(environ, 'panel')

    def service(previous=''):
        described = compose.panel_service(environ, image, text, {**facts, 'previous_node_hosts': previous},
                                          user=user, control_gid=control_gid)
        described['labels'] = {compose.SPEC_LABEL: compose.panel_spec(described, Path(CONFIG).read_bytes())}
        return described

    tx.candidate = service()
    kept = _kept(docker, tx.candidate['labels'][compose.SPEC_LABEL])
    if kept:
        say('панель не изменилась, контейнер оставлен')
    else:
        # Карта адресов заменяемой панели нужна обновлению с версии, которая ещё
        # не хранила адреса экзитов в базе.
        previous = compose.previous_node_hosts(tx.replace(image))
        db_owner()
        tx.candidate = service(previous)
        docker.publish(compose.PANEL, tx.candidate)
        docker.up(compose.PANEL)
        changed = True

    say('жду пока контейнер откроет HTTP…')
    http_up = engine.wait(lambda: engine.http_ok(HEALTH), 300, 0.2, **({'sleep': sleep} if sleep else {}))
    _diagnose(docker, say, http_up, kept)
    say('статус контейнера:')
    for line in docker.logs(compose.PANEL).splitlines():
        if 'client exit preference:' in line:
            print(line)
    status = docker.call('ps', '--filter', f'name={compose.PANEL}', '--format',
                         '{{.Names}} {{.Status}} {{.Ports}}', check=False).stdout.strip()
    say(f'    {status}')
    engine.stamp(STAGE)
    say(f'{STAGE} done')
    tx.finish()
    # Первый выпуск - после проверенной панели: неудача ACME не откатывает выпуск,
    # панель закрыта до сертификата, Caddy повторяет выпуск сам.
    subprocess.run([sys.executable, TLS, '--initial', '--config', TLS_CONFIG, '--wait',
                    environ.get('TLS_INITIAL_WAIT') or '60'], check=True)
    if subprocess.run(['systemctl', 'start', 'vpn2-tls.service'], check=False).returncode:
        say('vpn2-tls.service завершилась с ошибкой, подробности: journalctl -u vpn2-tls')
    subprocess.run(['systemctl', 'enable', '--now', 'vpn2-tls.timer'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _summary(environ, say, admin_user, facts['sso_enabled'])
    return changed


def run(environ, *, docker=None, say=engine.say, transaction=None, sleep=None):
    """Стадия целиком одним процессом: замок и откат охватывают её всю."""
    for name in ('HOST_TAG', 'ENTRY_HOST', 'AGH_PORT'):
        if not environ.get(name):
            raise StageError(f'{name} не задан')
    docker = docker or engine.Docker()
    os.makedirs(ETC, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    image = engine.release_image(environ, 'panel', environ.get('PANEL_IMAGE') or '',
                                 (environ.get('PANEL_IMAGE_PULL') or 'true') != 'false', docker=docker)
    say('[1/4] panel image digest verified')
    tx = transaction or Transaction(docker)

    def interrupted(number, _frame):
        raise SystemExit(128 + number)

    previous = {number: signal.signal(number, interrupted) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return _stage(environ, docker, say, tx, image, sleep)
    except BaseException:
        tx.failure()
        raise
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
