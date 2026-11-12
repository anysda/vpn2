"""Стадия 30-frontend: панель на 127.0.0.1:51821 за Caddy, замена транзакцией.

Неудача возвращает файлы хоста и прежний образ по digest; данные (база, отзывы, сроки, PKI)
остаются текущими, поэтому прежний образ запускается, только если понимает текущую схему базы.
"""

import fcntl
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

from vpn2 import fsutil, resources, svc
from vpn2.adguard.stage import admin_password, agh_password
from vpn2.containers import compose
from vpn2.containers.compose import PANEL, say

STAGE = '30-frontend'
ETC = '/etc/anysda'
CONFIG = f'{ETC}/anysda-config.yaml'
TEMPLATE = '/tmp/anysda/anysda-config.yaml.tpl'
SSO_RUNTIME = f'{ETC}/sso-runtime.json'
TLS = '/usr/local/lib/vpn2/vpn2_tls.py'
TLS_DIR = '/etc/vpn2/tls'
TLS_CONFIG = '/etc/vpn2/tls/config.json'
SECURE = '/etc/vpn2/tls/panel/secure'
MANAGED = '/etc/vpn2-control/api-managed'
CADDY = ['/etc/caddy/Caddyfile', '/etc/caddy/vpn2-tls.conf', '/etc/caddy/vpn2-http.conf', '/etc/caddy/vpn2-panel.conf']
OVPN_CONF = '/etc/openvpn/server/server.conf'
OVPN_USER = 'management-client-user vpn2-control'
OVPN_INPUTS = ['/usr/sbin/openvpn', '/usr/lib/systemd/system/openvpn-server@.service', OVPN_CONF,
               '/etc/openvpn/server/pki/ca.crt', '/etc/openvpn/server/pki/server.crt',
               '/etc/openvpn/server/pki/server.key', '/etc/openvpn/server/pki/tls-crypt.key']
APPLY = '/usr/local/sbin/vpn2-apply'
STATE = Path('/var/lib/vpn2-deploy/panel')
INSTALLED = Path('/var/lib/vpn2-deploy/installed.json')
DATA = '/var/lib/anysda-vpn2'
LEGACY_PREVIOUS = 'anysda-vpn2-previous'
HEALTH = 'http://127.0.0.1:51821/api/version'
NO_PREVIOUS = 'rollback refused: no previous release; repair the current installation'


class StageError(Exception):
    pass


def deploy_state(*args):
    return subprocess.run([sys.executable, '-m', 'vpn2.cli', 'state', *map(str, args)], check=False).returncode


def must(*args):
    if deploy_state(*args):
        raise StageError(f'deploy_state {args[-1] if args[0] != "--state" else args[2]}: отказ')


def healthy():
    return compose.wait(lambda: compose.http_ok(HEALTH), 30, 1)


class Transaction:
    def __init__(self, docker):
        self.docker = docker
        self.changed = self.old = self.replaced = False
        self.files, self.candidate, self.files_restored, self._lock = [], None, None, None

    def lock(self):
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = open(STATE / 'transaction.lock', 'a')  # noqa: SIM115 - замок живёт до конца процесса
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StageError('panel deploy already running') from None

    def begin(self, image):
        """Замок и проверка схемы образа; работающую панель не трогает."""
        self.lock()
        must('image-contract', '--image', image, '--contract', STATE / 'candidate-schema.json')
        must('compatible', '--contract', STATE / 'candidate-schema.json', '--migrating')
        self.changed = True

    def guard_files(self, files):
        """Файлы хоста, принадлежащие выпуску панели: неудачный деплой возвращает их."""
        pending = STATE / 'files.pending'
        shutil.rmtree(pending, ignore_errors=True)
        pending.mkdir(mode=0o700)
        for file in map(str, files):
            self.files.append(file)
            if os.path.lexists(file):
                _copy(file, pending / file.lstrip('/'))

    def restore_files(self):
        for file in self.files:
            saved = STATE / 'files.pending' / file.lstrip('/')
            if os.path.lexists(saved):
                _copy(saved, file)
            else:
                Path(file).unlink(missing_ok=True)
        if self.files:
            if self.files_restored:
                self.files_restored()
            print('panel host config restored')

    def replace(self, image):
        """Копия, снятие прежней панели и миграция базы под новую. Вернуть env прежней панели."""
        must('--state', STATE, 'backup')
        previous = STATE / 'previous-installed.json'
        if INSTALLED.is_file():
            shutil.copy(INSTALLED, previous)
        else:
            previous.unlink(missing_ok=True)
        shutil.rmtree(STATE / 'files', ignore_errors=True)
        if (STATE / 'files.pending').is_dir():
            shutil.copytree(STATE / 'files.pending', STATE / 'files', symlinks=True)
        self.replaced = True
        old = self.docker.inspect(PANEL)
        if old:
            must('image-contract', '--image', old['Image'], '--contract', STATE / 'previous-schema.json')
            fsutil.write_json(STATE / 'previous-image.json', {'id': old['Image']})
            self.old = True
            # Образ остаётся: перезагрузка посреди миграции не поднимет старый код на новой схеме.
            self.docker.call('rm', '-f', PANEL)
            self.docker.call('rm', '-f', LEGACY_PREVIOUS, check=False)
        print('administrative changes paused for migration; VPN services remain running')
        must('--state', STATE, 'backup')
        self.docker.call('run', '--rm', '--network', 'none', '--user', '0:0', '-v', f'{DATA}:{DATA}',
                         '-e', f'NUXT_DATABASE_URL={compose.DB_URL}', '--entrypoint', 'flock', image,
                         '-n', f'{DATA}/.migration.lock', 'node', 'scripts/migrate.mjs', capture_output=False)
        must('compatible', '--contract', STATE / 'candidate-schema.json')
        return compose.env_of(old)

    def previous_image(self):
        saved = fsutil.read_json(STATE / 'previous-image.json', default=None)
        if isinstance(saved, dict) and saved.get('id'):
            return saved['id']
        legacy = self.docker.inspect(LEGACY_PREVIOUS)
        return legacy['Image'] if legacy else ''

    def rollback(self):
        """Прежний образ панели по digest; данные остаются текущими."""
        contract = STATE / 'previous-schema.json'
        image = self.previous_image() if contract.is_file() else ''
        service = self.docker.services().get(PANEL) or self.candidate
        if not image or not self.docker.image_id(image) or not service:
            print(NO_PREVIOUS, file=sys.stderr)
            return False
        if deploy_state('compatible', '--contract', contract):
            print('rollback refused: incompatible schema; keep current DB/PKI and deploy a compatible image',
                  file=sys.stderr)
            return False
        try:
            self.docker.publish(PANEL, {**service, 'image': image, 'labels': {compose.SPEC_LABEL: 'rollback'}})
            self.docker.up(PANEL)
            self.docker.call('rm', '-f', LEGACY_PREVIOUS, check=False)
        except compose.EngineError as error:
            print(f'rollback failed: {error}', file=sys.stderr)
            return False
        if not healthy():
            print('rollback failed health check; inspect service status', file=sys.stderr)
            return False
        if deploy_state('record-rollback'):
            return False
        (STATE / 'previous-image.json').unlink(missing_ok=True)
        print('panel rollback verified; current database, revocations, expirations, operations and PKI retained')
        return True

    def failure(self):
        """Стадия упала после begin: вернуть файлы хоста и прежнюю панель."""
        if not self.changed:
            return
        self.changed = False
        try:
            self.restore_files()
        except OSError:
            print('panel host config restore failed', file=sys.stderr)
        shutil.rmtree(STATE / 'files.pending', ignore_errors=True)
        if self.old and not self.rollback():
            print('automatic rollback unavailable; deploy a schema-compatible release; '
                  'do not restore an old access snapshot', file=sys.stderr)
        elif not self.old and self.replaced:
            print('installation failed; no previous release exists', file=sys.stderr)

    def finish(self):
        if not healthy():
            raise StageError('панель не отвечает на /api/version')
        must('--state', STATE, 'phase', '--phase', 'complete')
        self.changed = False
        shutil.rmtree(STATE / 'files.pending', ignore_errors=True)


def _copy(source, target):
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)
    stat = os.lstat(source)
    os.chown(target, stat.st_uid, stat.st_gid, follow_symlinks=False)


def db_owner():
    """Миграция идёт от root и на первой установке создаёт базу с владельцем root."""
    for name in ('', 'db.sqlite', 'db.sqlite-wal', 'db.sqlite-shm'):
        if os.path.lexists(os.path.join(DATA, name)):
            shutil.chown(os.path.join(DATA, name), 'vpn2-panel', 'vpn2-panel')


def sso(environ):
    """Засев SSO из config.yaml; настроенный в профиле панели (sso-runtime.json) сильнее."""
    get = environ.get
    enabled = (get('SSO_ENABLED') or 'false') if (get('SSO_CLIENT_SECRET') or '').strip() else 'false'
    auto, password_login = get('SSO_AUTO_REDIRECT') or 'true', get('SSO_PASSWORD_LOGIN') or 'true'
    if enabled != 'true':
        auto, password_login = 'false', 'true'
    domain = get('PANEL_DOMAIN') or ''
    redirect = f'https://{domain}/auth/authentik' if enabled == 'true' and domain else ''
    if os.path.isfile(SSO_RUNTIME):
        say(f'  SSO: настроен в профиле панели ({SSO_RUNTIME}), блок sso: из config.yaml не применяется')
    elif enabled == 'true':
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


def write_config(user, password):
    """Логин и пароль JSON-строками: JSON-строка - YAML-скаляр, спецсимволы пароля конфиг не ломают."""
    template = Path(os.environ.get('VPN2_PANEL_CONFIG_TPL') or TEMPLATE)
    if not template.is_file():
        raise StageError(f'{template} не найден')
    text = (template.read_text().replace('${ADMIN_USER_YAML}', json.dumps(user, ensure_ascii=False))
            .replace('${ADMIN_PASS_YAML}', json.dumps(password, ensure_ascii=False)))
    changed = fsutil.publish(CONFIG, text, 0o640)
    shutil.chown(CONFIG, 'root', 'vpn2-panel')
    return changed


def session_secret():
    """Постоянный секрет сессий: куки переживают пересоздание контейнера."""
    path = Path(ETC, 'session-secret.txt')
    changed = not path.is_file() and fsutil.publish(path, secrets.token_hex(32) + '\n')
    return path.read_text().strip(), changed


CADDYFILE = '''{
    # :80 только переадресует на HTTPS; панель открыта лишь при сертификате от CA (vpn2-panel.conf).
    auto_https disable_redirects
    default_sni @TLS_HOST@
    # Без HTTP/3: Caddy держал бы *:443/udp, нужный sing-box для возврата UDP через tproxy.
    servers {
        protocols h1 h2
    }
}
# Пути бота - только для самой ноды, бот ходит на 127.0.0.1:51821 мимо Caddy.
(internal_only) {
    @internal path /api/bot /api/bot/* /api/ops/bot-snapshot
    respond @internal 404
}
# X-Forwarded-For ставит только Caddy: по нему панель считает лимит попыток входа.
(to_panel) {
    reverse_proxy 127.0.0.1:51821 {
        header_up X-Forwarded-For {remote_host}
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
    """management-client-user -> vpn2-control через vpn2-apply: last good получает тот же конфиг."""

    def __init__(self):
        self.switched = False

    @staticmethod
    def mark_restarted():
        """vpn2-apply уже перезапустил OpenVPN: стадия 29 второй раз этого не делает."""
        target = Path(svc.APPLIED, 'openvpn-server@server.sha256')
        target.parent.mkdir(parents=True, exist_ok=True)
        fsutil.atomic(target, svc.fingerprint(OVPN_INPUTS) + '\n', mode=0o644)

    def switch(self):
        conf = Path(OVPN_CONF)
        if not conf.is_file():
            return False
        lines = conf.read_text().splitlines(keepends=True)
        if OVPN_USER + '\n' in lines or OVPN_USER in lines:
            return False
        candidate = ''.join(OVPN_USER + '\n' if line.startswith('management-client-user ') else line for line in lines)
        with tempfile.NamedTemporaryFile('w', delete=False) as handle:
            handle.write(candidate)
        try:
            code = subprocess.run([APPLY, 'install', 'openvpn', handle.name], check=False).returncode
            # Прерванный apply мог уже заменить server.conf: решает содержимое, не код.
            self.switched = conf.read_text() == candidate
        finally:
            os.unlink(handle.name)
        if code:
            print(f'[{os.environ.get("HOST_TAG", "ru")}] vpn2-apply install openvpn: код {code}, '
                  f'server.conf {"уже заменён" if self.switched else "не изменён"}', file=sys.stderr)
            raise SystemExit(code)
        self.switched = True
        self.mark_restarted()
        return True

    def restored(self):
        """Файлы хоста возвращены: Caddy и OpenVPN перечитывают прежние."""
        subprocess.run(['systemctl', 'reload-or-restart', 'caddy'], check=False)
        if self.switched:
            subprocess.run(['systemctl', 'try-restart', 'openvpn-server@server'], check=False)
            if subprocess.run([APPLY, 'mark', 'openvpn'], check=False).returncode:
                print('vpn2-apply mark openvpn не прошёл: last good не совпадает с server.conf', file=sys.stderr)
            self.mark_restarted()
        if os.path.isfile('/etc/systemd/system/vpn2-tls.timer'):
            subprocess.run(['systemctl', 'start', 'vpn2-tls.timer'], check=False)


def host_tls(environ, tx):
    """Вставки TLS и Caddyfile под охраной выпуска панели; вернуть, менялся ли Caddyfile."""
    domain, entry = environ.get('PANEL_DOMAIN') or '', environ['ENTRY_HOST']
    if domain:
        host, site, out = domain, domain, f'{TLS_DIR}/domain'
        say(f"  домен панели: {domain} (HTTPS через Let's Encrypt)")
    else:
        host, site, out = entry, f'https://{entry}', f'{TLS_DIR}/ip'
        say(f"  домен не задан — HTTPS на IP {entry} (Let's Encrypt, ~6 дней)")
    tx.guard_files([*CADDY, TLS_CONFIG, SECURE, MANAGED, OVPN_CONF])
    subprocess.run(
        [sys.executable, TLS, '--prepare', '--role', 'panel', '--config', TLS_CONFIG, '--host', host,
         '--secure-file', SECURE, '--panel-container', PANEL, '--output-dir', out,
         '--acme-url', environ.get('TLS_ACME_URL') or 'https://acme-v02.api.letsencrypt.org/directory',
         '--acme-root', environ.get('TLS_ACME_ROOT') or ''], check=True)
    return fsutil.publish(CADDY[0], CADDYFILE.replace('@TLS_HOST@', host).replace('@SITE@', site), 0o644)


def caddy(regated):
    """Правила ufw и перезапуск Caddy при смене входов; вставку панели перечитывает сам vpn2_tls.py."""
    from vpn2 import firewall
    firewall.ufw_reconcile('caddy HTTP + ACME challenge', ['80/tcp'], ['caddy HTTP панель'])
    firewall.ufw_reconcile('caddy HTTPS панель', ['443/tcp'])
    if subprocess.run(['caddy', 'validate', '--config', CADDY[0]], check=False, capture_output=True).returncode:
        raise StageError('конфиг caddy невалиден')
    if regated:
        svc.forget('caddy')
    return svc.restart_if_changed('caddy', ['/usr/bin/caddy', '/usr/lib/systemd/system/caddy.service', *CADDY[:3]])


def diagnose(docker, http_up, kept):
    logs = '' if kept else docker.logs(PANEL)
    bad = [line for line in logs.splitlines() if 'anysda config ' in line and ' is invalid' in line]
    if bad:
        say(f'✗ панель не разобрала {CONFIG}, пароль админа не применён:')
        say(f'  {bad[-1][:400]}')
        raise StageError('')
    if not http_up or 'panel init failed' in logs:
        say('✗ панель не поднялась: /api/version не ответил 2xx за 60 с или init упал')
        status = docker.call('ps', '-a', '--filter', f'name=^{PANEL}$', '--format', '{{.Status}}', check=False)
        say(f'  контейнер: {status.stdout.strip()}')
        say('  хвост журнала панели:')
        for line in docker.logs(PANEL, tail=30).splitlines():
            say(f'    {line}')
        raise StageError('')


def summary(environ, admin_user, sso_enabled):
    domain = environ.get('PANEL_DOMAIN') or ''
    opened = any(line.startswith('import to_panel') for line in _read(CADDY[3]).splitlines())
    secret = f'{admin_user} / (пароль в {ETC}/admin-password.txt)'
    print()
    say('-------------------------------------------------------------')
    say(f' Панель:        https://{domain or environ["ENTRY_HOST"]}/{"" if opened else " (закрыта до выпуска сертификата)"}')
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


def quiet(*argv):
    subprocess.run(list(argv), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stage(environ, docker, tx, image):
    panel = pwd.getpwnam('vpn2-panel')
    control_gid = grp.getgrnam('vpn2-control').gr_gid
    user = f'{panel.pw_uid}:{panel.pw_gid}'
    # Кандидат сам проверяет свои требования к API под настоящим UID контейнера.
    docker.call('run', '--rm', '--network', 'none', '--user', user, '--group-add', str(control_gid),
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '-v', '/run/vpn2-control:/run/vpn2-control:ro',
                '--entrypoint', 'node', image, 'scripts/control-probe.mjs', capture_output=False)
    tx.begin(image)
    db_owner()

    say('[2/4] config')
    password, changed = admin_password(environ)
    admin_user = environ.get('ADMIN_USER') or 'anysda'
    facts = sso(environ)
    changed |= write_config(admin_user, password)
    session, created = session_secret()
    changed |= created
    say(f'  admin user:  {admin_user}')
    say(f'  admin pass:  (см. {ETC}/admin-password.txt на ноде)')

    say('[3/4] caddy')
    quiet('systemctl', 'stop', 'vpn2-tls.timer', 'vpn2-tls.service')
    openvpn = Openvpn()
    tx.files_restored = openvpn.restored
    gate = _read(CADDY[3])
    changed |= host_tls(environ, tx)
    changed |= caddy(regated=_read(CADDY[3]) != gate)

    say('[4/4] run container')
    stamp = compose.prepare_host(STAGE)
    quiet('systemctl', 'stop', 'anysda-ikev2-sync.timer', 'anysda-ikev2-sync.service')
    changed |= openvpn.switch()
    changed |= not Path(MANAGED).exists()
    Path(MANAGED).touch()
    quiet('systemctl', 'start', 'anysda-ikev2-sync.timer')
    facts.update(session=session, clash=_read(f'{ETC}/clash-secret.txt'), tgbot=_read(f'{ETC}/tgbot-secret.txt'),
                 admin_user=admin_user, agh_password=agh_password(password))
    values = resources.entry(environ)

    def service(previous=''):
        described = compose.panel_service(environ, image, values, {**facts, 'previous_node_hosts': previous},
                                          user=user, control_gid=control_gid)
        described['labels'] = {compose.SPEC_LABEL: compose.panel_spec(described, Path(CONFIG).read_bytes())}
        return described

    tx.candidate = service()
    current = docker.inspect(PANEL)
    kept = bool(current and current['State'].get('Running')
                and (current['Config'].get('Labels') or {}).get(compose.SPEC_LABEL) == tx.candidate['labels'][compose.SPEC_LABEL]
                and (current['Config'].get('Labels') or {}).get(compose.PROJECT_LABEL) == compose.PROJECT
                and compose.http_ok(HEALTH))
    if kept:
        say('панель не изменилась, контейнер оставлен')
    else:
        # Карта адресов заменяемой панели - для обновления с версии без адресов экзитов в базе.
        previous = tx.replace(image)
        db_owner()
        tx.candidate = service(previous.get('NUXT_PREVIOUS_NODE_HOSTS') or previous.get('NUXT_NODE_HOSTS') or '')
        docker.publish(PANEL, tx.candidate)
        docker.up(PANEL)
        changed = True

    say('жду пока контейнер откроет HTTP…')
    diagnose(docker, compose.wait(lambda: compose.http_ok(HEALTH), 300, 0.2), kept)
    say('статус контейнера:')
    for line in docker.logs(PANEL).splitlines():
        if 'client exit preference:' in line:
            print(line)
    status = docker.call('ps', '--filter', f'name={PANEL}', '--format', '{{.Names}} {{.Status}} {{.Ports}}', check=False)
    say(f'    {status.stdout.strip()}')
    stamp.touch()
    say(f'{STAGE} done')
    tx.finish()
    # Первый выпуск - после проверенной панели: неудача ACME выпуск не откатывает, Caddy повторит сам.
    subprocess.run([sys.executable, TLS, '--initial', '--config', TLS_CONFIG, '--wait',
                    environ.get('TLS_INITIAL_WAIT') or '60'], check=True)
    if subprocess.run(['systemctl', 'start', 'vpn2-tls.service'], check=False).returncode:
        say('vpn2-tls.service завершилась с ошибкой, подробности: journalctl -u vpn2-tls')
    subprocess.run(['systemctl', 'enable', '--now', 'vpn2-tls.timer'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    summary(environ, admin_user, facts['sso_enabled'])
    return changed


def run(environ, docker):
    """Стадия одним процессом: замок и откат охватывают её всю."""
    for name in ('HOST_TAG', 'ENTRY_HOST', 'AGH_PORT'):
        if not environ.get(name):
            raise StageError(f'{name} не задан')
    os.makedirs(ETC, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    image = compose.release_image(environ, 'panel', docker)
    say('[1/4] panel image digest verified')
    tx = Transaction(docker)

    def interrupted(number, _frame):
        raise SystemExit(128 + number)

    previous = {number: signal.signal(number, interrupted) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return stage(environ, docker, tx, image)
    except BaseException:
        tx.failure()
        raise
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
