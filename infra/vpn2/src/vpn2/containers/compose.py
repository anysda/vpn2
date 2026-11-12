"""Службы compose-проекта entry (/etc/vpn2/compose/compose.yaml) и вызовы docker."""

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from vpn2 import fsutil, release, resources

PROJECT = 'vpn2'
VM, PANEL, BOT = 'vmsingle', 'anysda-vpn2', 'anysda-tgbot'
SPEC_LABEL = 'vpn2.spec'
PROJECT_LABEL = 'com.docker.compose.project'
COMPOSE_FILE = '/etc/vpn2/compose/compose.yaml'
MANIFEST = '/tmp/anysda/release-manifest.json'
DB_URL = 'file:/var/lib/anysda-vpn2/db.sqlite'
PANEL_COMMAND = ['sh', '-ec', ('export NUXT_SESSION_COOKIE_SECURE=$(cat /run/vpn2-tls/secure); '
                              'exec node .output/server/index.mjs')]


class EngineError(Exception):
    pass


def say(text):
    print(f'[{os.environ.get("HOST_TAG") or "anysda"}] {text}', flush=True)


def upper(tag):
    return tag.upper().replace('-', '_')


def exit_tags(environ):
    return (environ.get('EXIT_TAGS') or '').split()


def _service(name, image, limits, **fields):
    return {'container_name': name, 'image': image, **limits, 'restart': 'always',
            'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}},
            'network_mode': 'host', **fields}


def _hardened(user, group):
    return {'user': user, 'group_add': [str(group)], 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges']}


def scrape_config(environ):
    """Цели VictoriaMetrics: entry по loopback, экзит через свой SOCKS-вход sing-box (10100 + октет)."""
    lines = ['global:', '  scrape_interval: 2s', '  scrape_timeout: 1s', '', 'scrape_configs:',
             '  - job_name: node-ru', '    static_configs:', "      - targets: ['127.0.0.1:9100']",
             "        labels: { host: ru, instance: '%s:9100' }" % (environ.get('MGMT_IP_RU') or '10.99.0.1')]
    skipped = []
    for tag in exit_tags(environ):
        address = environ.get('MGMT_IP_' + upper(tag)) or ''
        if not address:
            skipped.append(tag)
            continue
        lines += [f'  - job_name: node-{tag}', f"    proxy_url: 'socks5://127.0.0.1:{10100 + int(address.rsplit('.', 1)[-1])}'",
                  '    static_configs:', "      - targets: ['127.0.0.1:9100']",
                  f"        labels: {{ host: {tag}, instance: '{address}:9100' }}"]
    return '\n'.join(lines) + '\n', skipped


def vm_service(image, values, scrape):
    limits, _, runtime = resources.container(values, 'vm')
    return _service(
        VM, image, limits,
        labels={SPEC_LABEL: hashlib.sha256(scrape.encode()).hexdigest()},
        security_opt=['apparmor=unconfined'],
        volumes=['/etc/localtime:/etc/localtime:ro', '/var/lib/vmsingle:/storage',
                 '/etc/anysda/vmsingle-scrape.yml:/etc/vm/scrape.yml:ro'],
        command=['-storageDataPath=/storage', '-httpListenAddr=127.0.0.1:8428', *runtime,
                 '-search.latencyOffset=1s', '-promscrape.config=/etc/vm/scrape.yml'])


def bot_service(environ, image, values, *, secret, user, panel_gid):
    limits, extra, _ = resources.container(values, 'bot')
    env = {**extra, 'TELEGRAM_BOT_TOKEN': environ.get('TELEGRAM_BOT_TOKEN') or '',
           'TELEGRAM_CHAT_ID': environ.get('TELEGRAM_CHAT_ID') or '',
           'TELEGRAM_ADMIN_USERNAME': environ.get('TELEGRAM_ADMIN_USERNAME') or '',
           'TGBOT_SECRET': secret, 'TGBOT_EVENT_PORT': '8877',
           'TGBOT_POLL_STALL_SEC': environ.get('TGBOT_POLL_STALL_SEC') or '300',
           'ANYSDA_URL': 'http://127.0.0.1:51821', 'TG_PROXY': 'http://127.0.0.1:7897'}
    for name in ('ALERT_CPU_PCT', 'ALERT_RAM_PCT', 'ALERT_WINDOW_SEC', 'TGBOT_INVALID_TOKEN_PAUSE_SEC'):
        if environ.get(name):
            env[name] = environ[name]
    return _service(
        BOT, image, limits, **_hardened(user, panel_gid),
        volumes=['/etc/localtime:/etc/localtime:ro',
                 '/var/lib/anysda-vpn2/runtime/telegram-runtime.json:/etc/anysda/telegram-runtime.json:ro',
                 '/var/lib/anysda-tgbot:/var/lib/anysda-tgbot'],
        environment=env)


def node_hosts(environ):
    """Публичные адреса нод для карточек мониторинга: «ru=<entry>,<тег>=<адрес>»."""
    return ','.join(['ru=' + environ['ENTRY_HOST'], *(f'{tag}={environ.get("DOMAIN_" + upper(tag)) or ""}'
                                                      for tag in exit_tags(environ))])


def mgmt_ips(environ):
    pairs = [(tag, environ.get('MGMT_IP_' + upper(tag)) or '') for tag in exit_tags(environ)]
    return ','.join(['ru:' + (environ.get('MGMT_IP_RU') or '10.99.0.1'), *(f'{tag}:{ip}' for tag, ip in pairs if ip)])


def panel_environment(environ, facts):
    """facts: секреты и решения стадии (session, clash, tgbot, admin_user, agh_password, sso*)."""
    entry, get = environ['ENTRY_HOST'], environ.get
    return {
        'NODE_ENV': 'production', 'PORT': '51821', 'HOST': '127.0.0.1',
        'NUXT_SESSION_PASSWORD': facts['session'], 'NUXT_DATABASE_URL': DB_URL,
        'NUXT_ANYSDA_CONFIG_PATH': '/etc/anysda/config.yaml', 'NUXT_WG_ENABLED': 'true',
        'NUXT_WG_LISTEN_PORT': get('WG_LISTEN_PORT') or '51820',
        'NUXT_WG_SERVER_IP': get('WG_SERVER_IP') or '10.66.66.1',
        'NUXT_WG_SUBNET_PREFIX': get('WG_SUBNET_PREFIX') or '10.66.66.',
        'NUXT_WG_PUBLIC_HOST': get('WG_PUBLIC_HOST') or entry, 'NUXT_WG_ENDPOINT_IP': entry,
        'NUXT_NODE_HOSTS': node_hosts(environ), 'NUXT_PREVIOUS_NODE_HOSTS': facts.get('previous_node_hosts', ''),
        'NUXT_WG_MTU': get('WG_MTU') or '1420', 'NUXT_WG_SPLIT_LOCAL': get('WG_SPLIT_LOCAL') or 'true',
        'NUXT_OVPN_ENABLED': 'true', 'NUXT_OVPN_PORT': get('OVPN_PORT') or '1194',
        'NUXT_OVPN_PROTO': get('OVPN_PROTO') or 'udp', 'NUXT_OVPN_PUBLIC_HOST': get('OVPN_PUBLIC_HOST') or entry,
        'NUXT_ROUTES_FILE_PATH': '/etc/anysda/manual-routes.json', 'NUXT_CLASH_SECRET': facts['clash'],
        'NUXT_CLASH_API_URL': f'http://{get("MGMT_IP") or ""}:9090',
        'NUXT_AGH_URL': f'http://127.0.0.1:{environ["AGH_PORT"]}', 'NUXT_AGH_USER': facts['admin_user'],
        'NUXT_AGH_PASSWORD': facts['agh_password'], 'NUXT_EXIT_TAGS': get('EXIT_TAGS') or '',
        'NUXT_MGMT_MESH_IP_PREFIX': '10.99.0.', 'NUXT_MGMT_IPS': mgmt_ips(environ),
        'NUXT_VM_URL': 'http://127.0.0.1:8428', 'NUXT_TGBOT_SECRET': facts['tgbot'], 'NUXT_TGBOT_EVENT_PORT': '8877',
        'NUXT_PUBLIC_SSO_ENABLED': facts['sso_enabled'], 'NUXT_PUBLIC_SSO_AUTO_REDIRECT': facts['sso_auto_redirect'],
        'NUXT_PUBLIC_SSO_LABEL': get('SSO_LABEL') or 'Authentik', 'NUXT_SSO_ALLOWED_SUBS': get('SSO_ALLOWED_SUBS') or '',
        'NUXT_SSO_PASSWORD_LOGIN': facts['sso_password_login'],
        'NUXT_OAUTH_OIDC_CLIENT_ID': get('SSO_CLIENT_ID') or '',
        'NUXT_OAUTH_OIDC_CLIENT_SECRET': get('SSO_CLIENT_SECRET') or '',
        'NUXT_OAUTH_OIDC_OPENID_CONFIG': get('SSO_DISCOVERY_URL') or '',
        'NUXT_OAUTH_OIDC_REDIRECT_URL': facts['sso_redirect_url'], 'LOG_LEVEL': 'info',
    }


def panel_service(environ, image, values, facts, *, user, control_gid):
    limits, extra, _ = resources.container(values, 'panel')
    return _service(
        PANEL, image, limits, **_hardened(user, control_gid),
        volumes=['/etc/localtime:/etc/localtime:ro', '/etc/vpn2/tls/panel:/run/vpn2-tls:ro',
                 '/etc/anysda/anysda-config.yaml:/etc/anysda/config.yaml:ro',
                 '/run/vpn2-control:/run/vpn2-control:ro', '/var/lib/anysda-vpn2:/var/lib/anysda-vpn2'],
        environment={**extra, **panel_environment(environ, facts)}, command=list(PANEL_COMMAND))


def panel_spec(service, config):
    """Отпечаток запуска панели: служба без метки и карты прежних адресов, и конфиг, читаемый при старте."""
    plain = json.loads(json.dumps(service))
    plain.pop('labels', None)
    plain['environment']['NUXT_PREVIOUS_NODE_HOSTS'] = ''
    return hashlib.sha256(json.dumps(plain, sort_keys=True, ensure_ascii=False).encode() + b'\0' + config).hexdigest()


def env_of(container):
    return dict(item.split('=', 1) for item in (container or {}).get('Config', {}).get('Env') or [] if '=' in item)


def _strings(value, edit):
    if isinstance(value, str):
        return edit(value)
    if isinstance(value, list):
        return [_strings(item, edit) for item in value]
    if isinstance(value, dict):
        return {key: _strings(item, edit) for key, item in value.items()}
    return value


def load(text):
    """Службы из текста compose.yaml; `$$` (подстановка compose) обратно в `$`."""
    try:
        data = yaml.safe_load(text or '') or {}
    except yaml.YAMLError as error:
        raise EngineError(f'compose.yaml не разобран: {error}') from error
    services = data.get('services') if isinstance(data, dict) else None
    return _strings(dict(services), lambda value: value.replace('$$', '$')) if isinstance(services, dict) else {}


def with_service(text, name, service):
    """Текст compose.yaml с заменённой службой, службы в постоянном порядке; и признак смены."""
    services = {**load(text), name: service}
    ordered = {key: _strings(services[key], lambda value: value.replace('$', '$$'))
               for key in (VM, PANEL, BOT) if key in services}
    fresh = yaml.safe_dump({'name': PROJECT, 'services': ordered}, sort_keys=False,
                           allow_unicode=True, default_flow_style=False, width=10 ** 6)
    return fresh, fresh != text


class Docker:
    def __init__(self, compose_file=COMPOSE_FILE):
        self.compose_file = str(compose_file)

    def call(self, *args, check=True, capture_output=True):
        result = subprocess.run(['docker', *args], check=False, capture_output=capture_output, text=True)
        if check and result.returncode:
            detail = (result.stderr or '').strip().splitlines()[-1:] or ['']
            raise EngineError(f'docker {" ".join(args[:2])}: код {result.returncode} {detail[0][:300]}')
        return result

    def inspect(self, name):
        result = self.call('inspect', '--type', 'container', name, check=False)
        return json.loads(result.stdout)[0] if result.returncode == 0 else None

    def image_id(self, reference):
        result = self.call('image', 'inspect', '--format', '{{.Id}}', reference, check=False)
        return result.stdout.strip() if result.returncode == 0 else ''

    def logs(self, name, tail=None):
        result = self.call('logs', *(['--tail', str(tail)] if tail else []), name, check=False)
        return (result.stdout or '') + (result.stderr or '')

    def services(self):
        try:
            return load(Path(self.compose_file).read_text())
        except OSError:
            return {}

    def publish(self, name, service):
        """Записать службу в compose.yaml; вернуть, сменился ли файл."""
        path = Path(self.compose_file)
        fresh, changed = with_service(path.read_text() if path.is_file() else '', name, service)
        if changed:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fsutil.atomic(path, fresh, mode=0o600)
        return changed

    def up(self, name):
        """Контейнер по описанию службы; вернуть, создан ли он заново.

        Контейнер с тем же именем не из compose (docker run v1.1.5) снимается: данные в каталогах хоста.
        """
        before = self.inspect(name)
        if before and (before['Config'].get('Labels') or {}).get(PROJECT_LABEL) != PROJECT:
            self.call('rm', '-f', name)
        self.call('compose', '-f', self.compose_file, 'up', '-d', '--no-deps', name)
        after = self.inspect(name)
        if after is None:
            raise EngineError(f'{name}: контейнера нет после compose up')
        return before is None or before['Id'] != after['Id']


def release_image(environ, component, docker):
    """Образ компонента: выпуск по digest манифеста, в разработке - тег :dev или заданный."""
    requested = environ.get(f'{component.upper()}_IMAGE') or ''
    pull = (environ.get(f'{component.upper()}_IMAGE_PULL') or 'true') != 'false'
    if environ.get('VPN2_DEVELOPMENT', 'false') != 'true':
        return release.image(release.load(environ.get('VPN2_RELEASE_MANIFEST') or MANIFEST), component, requested, pull)
    reference = requested or f'ghcr.io/anysda/vpn2/{component}:dev'
    source = 'pulled'
    if not pull:
        source = 'local, pull disabled'
    elif docker.call('pull', reference, check=False).returncode:
        source = 'local, pull failed'
        print(f'WARNING: [development] {component}: pull {reference} failed, using the local image', file=sys.stderr)
    image = docker.image_id(reference)
    if not image:
        raise EngineError(f'{component}: образа {reference} нет')
    revision = docker.call('image', 'inspect', '--format', '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                           reference, check=False).stdout.strip()
    print(f'[development] {component}: {reference} revision {revision or "unknown"} ({source})', file=sys.stderr)
    return image


def http_ok(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait(probe, attempts, pause):
    for attempt in range(attempts):
        if probe():
            return True
        if attempt + 1 < attempts:
            time.sleep(pause)
    return False


def prepare_host(stage):
    """Путь отметки стадии. /etc/timezone каталогом (его создаёт docker на Ubuntu 26.04) не даёт контейнеру стартовать."""
    with contextlib.suppress(OSError):
        os.rmdir('/etc/timezone')
    Path('/var/anysda/.stamps').mkdir(parents=True, exist_ok=True)
    return Path('/var/anysda/.stamps', stage)
