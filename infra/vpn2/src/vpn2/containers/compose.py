"""Описание служб compose.yaml из окружения стадий; чистые функции без docker."""

import hashlib
import json
import shlex

import yaml

PROJECT = 'vpn2'
VM, PANEL, BOT = 'vmsingle', 'anysda-vpn2', 'anysda-tgbot'
SPEC_LABEL = 'vpn2.spec'
LOGGING = {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}
DB_URL = 'file:/var/lib/anysda-vpn2/db.sqlite'
PANEL_COMMAND = ['sh', '-ec', ('export NUXT_SESSION_COOKIE_SECURE=$(cat /run/vpn2-tls/secure); '
                              'exec node .output/server/index.mjs')]


class ComposeError(Exception):
    pass


def upper(tag):
    return tag.upper().replace('-', '_')


def exit_tags(environ):
    return (environ.get('EXIT_TAGS') or '').split()


def resources(text):
    """Вывод entry_resources --service: (ограничения службы, env, аргументы программы)."""
    arrays = {}
    for line in text.splitlines():
        name, _, value = line.partition('=')
        if value.startswith('(') and value.endswith(')'):
            arrays[name] = shlex.split(value[1:-1])
    limits, env = {}, {}
    args = iter(arrays.get('ENTRY_DOCKER_ARGS', []))
    for flag in args:
        value = next(args, None)
        if value is None:
            raise ComposeError(f'флаг ресурсов без значения: {flag}')
        if flag == '--memory':
            limits['mem_limit'] = value
        elif flag == '--memory-swap':
            limits['memswap_limit'] = value
        elif flag == '-e':
            name, _, text_value = value.partition('=')
            env[name] = text_value
        else:
            raise ComposeError(f'неизвестный флаг ресурсов: {flag}')
    return limits, env, arrays.get('ENTRY_RUNTIME_ARGS', [])


def _service(name, image, limits, **fields):
    service = {'container_name': name, 'image': image, **limits, 'restart': 'always',
               'logging': json.loads(json.dumps(LOGGING)), 'network_mode': 'host'}
    service.update(fields)
    return service


def scrape_config(environ):
    """Цели VictoriaMetrics: entry по loopback, экзит через свой SOCKS-вход sing-box."""
    lines = ['global:', '  scrape_interval: 2s', '  scrape_timeout: 1s', '', 'scrape_configs:',
             '  - job_name: node-ru', '    static_configs:', "      - targets: ['127.0.0.1:9100']",
             "        labels: { host: ru, instance: '%s:9100' }" % (environ.get('MGMT_IP_RU') or '10.99.0.1')]
    skipped = []
    for tag in exit_tags(environ):
        address = environ.get('MGMT_IP_' + upper(tag)) or ''
        if not address:
            skipped.append(tag)
            continue
        port = 10100 + int(address.rsplit('.', 1)[-1])
        lines += [f'  - job_name: node-{tag}', f"    proxy_url: 'socks5://127.0.0.1:{port}'",
                  '    static_configs:', "      - targets: ['127.0.0.1:9100']",
                  f"        labels: {{ host: {tag}, instance: '{address}:9100' }}"]
    return '\n'.join(lines) + '\n', skipped


def vm_service(image, resource_text, scrape):
    limits, _, runtime = resources(resource_text)
    return _service(
        VM, image, limits,
        labels={SPEC_LABEL: hashlib.sha256(scrape.encode()).hexdigest()},
        security_opt=['apparmor=unconfined'],
        volumes=['/etc/localtime:/etc/localtime:ro', '/var/lib/vmsingle:/storage',
                 '/etc/anysda/vmsingle-scrape.yml:/etc/vm/scrape.yml:ro'],
        command=['-storageDataPath=/storage', '-httpListenAddr=127.0.0.1:8428', *runtime,
                 '-search.latencyOffset=1s', '-promscrape.config=/etc/vm/scrape.yml'])


def _hardened(user, groups):
    return {'user': user, 'group_add': [str(group) for group in groups], 'cap_drop': ['ALL'],
            'security_opt': ['no-new-privileges']}


def bot_service(environ, image, resource_text, *, secret, user, panel_gid):
    limits, extra, _ = resources(resource_text)
    env = {
        **extra,
        'TELEGRAM_BOT_TOKEN': environ.get('TELEGRAM_BOT_TOKEN') or '',
        'TELEGRAM_CHAT_ID': environ.get('TELEGRAM_CHAT_ID') or '',
        'TELEGRAM_ADMIN_USERNAME': environ.get('TELEGRAM_ADMIN_USERNAME') or '',
        'TGBOT_SECRET': secret,
        'TGBOT_EVENT_PORT': '8877',
        'TGBOT_POLL_STALL_SEC': environ.get('TGBOT_POLL_STALL_SEC') or '300',
        'ANYSDA_URL': 'http://127.0.0.1:51821',
        'TG_PROXY': 'http://127.0.0.1:7897',
    }
    for name in ('ALERT_CPU_PCT', 'ALERT_RAM_PCT', 'ALERT_WINDOW_SEC', 'TGBOT_INVALID_TOKEN_PAUSE_SEC'):
        if environ.get(name):
            env[name] = environ[name]
    return _service(
        BOT, image, limits, **_hardened(user, [panel_gid]),
        volumes=['/etc/localtime:/etc/localtime:ro',
                 '/var/lib/anysda-vpn2/runtime/telegram-runtime.json:/etc/anysda/telegram-runtime.json:ro',
                 '/var/lib/anysda-tgbot:/var/lib/anysda-tgbot'],
        environment=env)


def node_hosts(environ):
    """Публичные адреса нод для карточек мониторинга: «ru=<entry>,<тег>=<адрес>»."""
    pairs = ['ru=' + environ['ENTRY_HOST']]
    pairs += [f'{tag}={environ.get("DOMAIN_" + upper(tag)) or ""}' for tag in exit_tags(environ)]
    return ','.join(pairs)


def mgmt_ips(environ):
    pairs = ['ru:' + (environ.get('MGMT_IP_RU') or '10.99.0.1')]
    for tag in exit_tags(environ):
        address = environ.get('MGMT_IP_' + upper(tag)) or ''
        if address:
            pairs.append(f'{tag}:{address}')
    return ','.join(pairs)


def panel_environment(environ, facts):
    """facts: секреты и решения стадии (session, clash, tgbot, admin_user, agh_password, sso*)."""
    entry = environ['ENTRY_HOST']
    get = environ.get
    return {
        'NODE_ENV': 'production', 'PORT': '51821', 'HOST': '127.0.0.1',
        'NUXT_SESSION_PASSWORD': facts['session'],
        'NUXT_DATABASE_URL': DB_URL,
        'NUXT_ANYSDA_CONFIG_PATH': '/etc/anysda/config.yaml',
        'NUXT_WG_ENABLED': 'true',
        'NUXT_WG_LISTEN_PORT': get('WG_LISTEN_PORT') or '51820',
        'NUXT_WG_SERVER_IP': get('WG_SERVER_IP') or '10.66.66.1',
        'NUXT_WG_SUBNET_PREFIX': get('WG_SUBNET_PREFIX') or '10.66.66.',
        'NUXT_WG_PUBLIC_HOST': get('WG_PUBLIC_HOST') or entry,
        'NUXT_WG_ENDPOINT_IP': entry,
        'NUXT_NODE_HOSTS': node_hosts(environ),
        'NUXT_PREVIOUS_NODE_HOSTS': facts.get('previous_node_hosts', ''),
        'NUXT_WG_MTU': get('WG_MTU') or '1420',
        'NUXT_WG_SPLIT_LOCAL': get('WG_SPLIT_LOCAL') or 'true',
        'NUXT_OVPN_ENABLED': 'true',
        'NUXT_OVPN_PORT': get('OVPN_PORT') or '1194',
        'NUXT_OVPN_PROTO': get('OVPN_PROTO') or 'udp',
        'NUXT_OVPN_PUBLIC_HOST': get('OVPN_PUBLIC_HOST') or entry,
        'NUXT_ROUTES_FILE_PATH': '/etc/anysda/manual-routes.json',
        'NUXT_CLASH_SECRET': facts['clash'],
        'NUXT_CLASH_API_URL': f'http://{get("MGMT_IP") or ""}:9090',
        'NUXT_AGH_URL': f'http://127.0.0.1:{environ["AGH_PORT"]}',
        'NUXT_AGH_USER': facts['admin_user'],
        'NUXT_AGH_PASSWORD': facts['agh_password'],
        'NUXT_EXIT_TAGS': get('EXIT_TAGS') or '',
        'NUXT_MGMT_MESH_IP_PREFIX': '10.99.0.',
        'NUXT_MGMT_IPS': mgmt_ips(environ),
        'NUXT_VM_URL': 'http://127.0.0.1:8428',
        'NUXT_TGBOT_SECRET': facts['tgbot'],
        'NUXT_TGBOT_EVENT_PORT': '8877',
        'NUXT_PUBLIC_SSO_ENABLED': facts['sso_enabled'],
        'NUXT_PUBLIC_SSO_AUTO_REDIRECT': facts['sso_auto_redirect'],
        'NUXT_PUBLIC_SSO_LABEL': get('SSO_LABEL') or 'Authentik',
        'NUXT_SSO_ALLOWED_SUBS': get('SSO_ALLOWED_SUBS') or '',
        'NUXT_SSO_PASSWORD_LOGIN': facts['sso_password_login'],
        'NUXT_OAUTH_OIDC_CLIENT_ID': get('SSO_CLIENT_ID') or '',
        'NUXT_OAUTH_OIDC_CLIENT_SECRET': get('SSO_CLIENT_SECRET') or '',
        'NUXT_OAUTH_OIDC_OPENID_CONFIG': get('SSO_DISCOVERY_URL') or '',
        'NUXT_OAUTH_OIDC_REDIRECT_URL': facts['sso_redirect_url'],
        'LOG_LEVEL': 'info',
    }


def panel_service(environ, image, resource_text, facts, *, user, control_gid):
    limits, extra, _ = resources(resource_text)
    return _service(
        PANEL, image, limits, **_hardened(user, [control_gid]),
        volumes=['/etc/localtime:/etc/localtime:ro', '/etc/vpn2/tls/panel:/run/vpn2-tls:ro',
                 '/etc/anysda/anysda-config.yaml:/etc/anysda/config.yaml:ro',
                 '/run/vpn2-control:/run/vpn2-control:ro', '/var/lib/anysda-vpn2:/var/lib/anysda-vpn2'],
        environment={**extra, **panel_environment(environ, facts)},
        command=list(PANEL_COMMAND))


def panel_spec(service, config):
    """Отпечаток запуска панели: служба с образом и конфиг, который она читает при старте.

    Карта прежних адресов и сама метка в отпечаток не входят: карта берётся из заменяемой панели.
    """
    plain = json.loads(json.dumps(service))
    plain.pop('labels', None)
    plain['environment']['NUXT_PREVIOUS_NODE_HOSTS'] = ''
    body = json.dumps(plain, sort_keys=True, ensure_ascii=False).encode() + b'\0' + config
    return hashlib.sha256(body).hexdigest()


def previous_node_hosts(env):
    """Карта адресов заменяемой панели: её прежняя карта, если есть, иначе текущая."""
    values = dict(item.split('=', 1) for item in env if '=' in item)
    return values.get('NUXT_PREVIOUS_NODE_HOSTS') or values.get('NUXT_NODE_HOSTS') or ''


def load(text):
    """Службы из текста compose.yaml; пустой или чужой файл - служб нет."""
    try:
        data = yaml.safe_load(text or '') or {}
    except yaml.YAMLError as error:
        raise ComposeError(f'compose.yaml не разобран: {error}') from error
    services = data.get('services') if isinstance(data, dict) else None
    return dict(services) if isinstance(services, dict) else {}


def render(services):
    """Текст compose.yaml: службы в постоянном порядке, значения env строками."""
    ordered = {name: services[name] for name in (VM, PANEL, BOT) if name in services}
    return yaml.safe_dump({'name': PROJECT, 'services': ordered}, sort_keys=False,
                          allow_unicode=True, default_flow_style=False, width=10 ** 6)


def _strings(value, edit):
    if isinstance(value, str):
        return edit(value)
    if isinstance(value, list):
        return [_strings(item, edit) for item in value]
    if isinstance(value, dict):
        return {key: _strings(item, edit) for key, item in value.items()}
    return value


def escape(service):
    """`$` в значениях compose читает как подстановку: удваиваем во всех строках службы."""
    return _strings(service, lambda text: text.replace('$', '$$'))


def unescape(service):
    return _strings(service, lambda text: text.replace('$$', '$'))


def with_service(text, name, service):
    """Текст compose.yaml с заменённой службой и признак, что текст сменился."""
    services = load(text)
    services[name] = escape(service)
    fresh = render(services)
    return fresh, fresh != text
