"""Мастер установки в браузере: служба root на 127.0.0.1 за Caddy, до venv - системным python3 из дерева.

./deploy.sh на чистой системе ставит его (vpn2.deploy.bootstrap) и печатает ссылку https://<IP>/#<токен>. Страница
меняет токен на cookie (POST /api/session), черновик хранит здесь без секретов, секреты кладёт в хранилище entry
(пароль админа - в файл передачи HANDOVER, его стирает деплой после стадии панели), установку запускает менеджер
(vpn2.manager). После done итог читается 10 минут, затем мастер снимает свой сайт и выключается навсегда.
HTTP API - строка «contract: wiz» в state/contracts.md полосы; отказ - {refused: текст для человека}.
"""
import contextlib
import hashlib
import http.server
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from vpn2 import fsutil

STATE = Path(os.environ.get('VPN2_WIZARD_STATE') or '/var/lib/vpn2-wizard')
RUN = Path(os.environ.get('VPN2_WIZARD_RUN') or '/run/vpn2-wizard')
STATIC = Path(os.environ.get('VPN2_WIZARD_STATIC') or '/opt/vpn2-wizard/static')
HANDOVER = STATE / 'handover'
CADDY = Path('/etc/caddy')
UNIT, PORT, COOKIE = 'vpn2-wizard', 51840, 'vpn2_wizard'
LINK_LIFE, DONE_LIFE, IDLE_AFTER_DONE = 3600, 600, 60
ATTEMPTS, ATTEMPT_WINDOW = 10, 600
SECRETS = {'admin_password': '', 'telegram_bot_token': 'telegram_bot_token', 's3_access_key': 's3_access_key',
           's3_secret_key': 's3_secret_key'}
EXPIRED = 'Ссылка устарела - запустите ./deploy.sh ещё раз'
DRAFT = {'step': 1, 'admin_ssh_key': '', 'exits': [], 'panel': {'domain': '', 'admin_user': 'anysda'},
         'telegram': {'enabled': False, 'chat_id': ''}, 'backup': {'target': 'local', 's3': {'endpoint': '', 'bucket': '', 'region': ''}},
         'youtube': False}
DOMAIN = re.compile(r'(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')


class Refusal(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def new_link():
    """Новый токен ссылки: прежний и все сессии мертвы. Вернуть токен (только в терминал)."""
    token = secrets.token_urlsafe(32)
    RUN.mkdir(mode=0o700, parents=True, exist_ok=True)
    with fsutil.lock(RUN / '.lock'):
        fsutil.atomic(RUN / 'token.json', json.dumps({'token': sha(token), 'expires': time.time() + LINK_LIFE,
                                                       'sessions': {}}), 0o600)
    return token


def wanted(argv, environ=os.environ):
    """./deploy.sh идёт мастером: без действий (флаги --development/--manifest не в счёт), не задание менеджера,
    мастер уже начат - или чистая система без config.yaml. config.yaml без мастера - установка по нему (-yaml)."""
    from vpn2.deploy import CONFIG, installed
    if environ.get('VPN2_JOB') or argv:
        return False
    if (STATE / 'draft.json').exists():
        from vpn2.manager import Manager
        number = read_json(STATE / 'state.json', {}).get('job')
        job = Manager().load(number) if number else None
        return not (job and job['state'] == 'done')
    return not (installed() or CONFIG.exists())


class Wizard:
    def __init__(self):
        self.lock, self.failures, self.seen = threading.RLock(), {}, 0.0
        from vpn2.deploy import CONFIG
        self.config = CONFIG

    # --- состояние на диске ---
    def draft(self):
        found = read_json(STATE / 'draft.json', {})
        return {**DRAFT, **{key: value for key, value in found.items() if key in DRAFT}}

    def save(self, draft):
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        fsutil.write_json(STATE / 'draft.json', draft)

    def meta(self):
        return read_json(STATE / 'state.json', {})

    def set_meta(self, **values):
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        fsutil.write_json(STATE / 'state.json', {**self.meta(), **values})

    def tokens(self):
        return read_json(RUN / 'token.json', {'token': '', 'expires': 0, 'sessions': {}})

    def write_tokens(self, data):
        with fsutil.lock(RUN / '.lock'):
            current = self.tokens()
            if current.get('token') == data.get('token'):
                fsutil.atomic(RUN / 'token.json', json.dumps(data), 0o600)

    # --- менеджер ---
    def manager(self, method, params=None):
        from vpn2 import manager
        try:
            reply = manager.call(method, params or {}, timeout=40)
        except (OSError, ValueError):
            raise Refusal(503, 'Служба установки перезапускается - повторите через несколько секунд') from None
        if not reply.get('ok'):
            raise Refusal(400, 'Служба установки не приняла запрос')
        if 'refused' in reply['result']:
            raise Refusal(409, reply['result']['refused'])
        return reply['result']

    def job(self):
        number = self.meta().get('job')
        return self.manager('job.read', {'job': number}) if number else None

    def phase(self, job):
        return 'filling' if not job else 'done' if job['state'] == 'done' else 'installing'

    # --- сессии ---
    def session(self, cookie, now):
        data = self.tokens()
        found = data['sessions'].get(sha(cookie)) if cookie else None
        if not found or found['expires'] and found['expires'] < now:
            raise Refusal(401, EXPIRED)
        self.seen = now
        return data

    def login(self, token, address, now):
        recent = [moment for moment in self.failures.get(address, []) if moment > now - ATTEMPT_WINDOW]
        self.failures[address] = recent
        if len(recent) >= ATTEMPTS:
            raise Refusal(429, 'Слишком много попыток - подождите 10 минут')
        data, meta = self.tokens(), self.meta()
        if (type(token) is not str or not data['token'] or not secrets.compare_digest(sha(token), data['token'])
                or data['expires'] < now or meta.get('done_at') or meta.get('job') and self.phase(self.job()) == 'done'):
            recent.append(now)
            raise Refusal(401, EXPIRED)
        cookie = secrets.token_urlsafe(32)
        # После начала установки сессия живёт до конца установки, до него - час ссылки.
        data['sessions'][sha(cookie)] = {'expires': None if self.meta().get('job') else data['expires']}
        self.write_tokens(data)
        return cookie

    def installing(self):
        """Установка пошла: живые сессии - до её конца."""
        with fsutil.lock(RUN / '.lock'):
            data = self.tokens()
            for found in data['sessions'].values():
                found['expires'] = None
            fsutil.atomic(RUN / 'token.json', json.dumps(data), 0o600)

    # --- черновик ---
    def check_draft(self, part):
        from vpn2 import config, nodevars
        from vpn2.deploy import key_id
        out = {}
        for key, value in part.items():
            if key not in DRAFT:
                raise Refusal(400, f'Неизвестное поле {key}')
            if key == 'step':
                if type(value) is not int or not 1 <= value <= 8:
                    raise Refusal(400, 'Поле step: номер экрана 1-8')
            elif key == 'admin_ssh_key':
                if type(value) is not str or value.strip() and ('\n' in value.strip() or not key_id(value)):
                    raise Refusal(400, 'Ключ администратора: одна строка открытого ключа ssh, «ssh-ed25519 AAAA… имя»')
                value = value.strip()
            elif key == 'exits':
                value = self.check_exits(value, config, nodevars)
            elif key == 'panel':
                domain, user = self.fields(value, 'panel', {'domain': str, 'admin_user': str})
                domain = domain.strip().lower().rstrip('.')
                if domain and not DOMAIN.fullmatch(domain):
                    raise Refusal(400, f'Домен «{domain}»: имя вида vpn.example.com, без http:// и пути')
                if not re.fullmatch(r'[a-z_][a-z0-9_.-]{0,31}', user):
                    raise Refusal(400, 'Логин администратора: строчные латинские буквы, цифры, «_», «-», «.», до 32 знаков')
                value = {'domain': domain, 'admin_user': user}
            elif key == 'telegram':
                enabled, chat = self.fields(value, 'telegram', {'enabled': bool, 'chat_id': str})
                if chat.strip() and not re.fullmatch(r'-?\d{1,20}', chat.strip()):
                    raise Refusal(400, 'Чат Telegram: число, например 123456789 или -1001234567890')
                value = {'enabled': enabled, 'chat_id': chat.strip()}
            elif key == 'backup':
                target, s3 = self.fields(value, 'backup', {'target': str, 's3': dict})
                endpoint, bucket, region = self.fields(s3, 'backup.s3', {'endpoint': str, 'bucket': str, 'region': str})
                if target not in ('local', 's3'):
                    raise Refusal(400, 'Бэкап: на этом сервере (local) или в S3 (s3)')
                if endpoint and not re.fullmatch(r'https://[A-Za-z0-9.-]+(:\d{1,5})?(/[!-~]*)?', endpoint.strip()):
                    raise Refusal(400, 'Адрес S3: https://…, например https://s3.eu-central-1.amazonaws.com')
                if bucket and not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket.strip()):
                    raise Refusal(400, 'Бакет S3: 3-63 строчных латинских букв, цифр, точек и дефисов')
                if region and not re.fullmatch(r'[a-z0-9-]{1,32}', region.strip()):
                    raise Refusal(400, 'Регион S3: например eu-central-1')
                value = {'target': target, 's3': {'endpoint': endpoint.strip(), 'bucket': bucket.strip(), 'region': region.strip()}}
            elif type(value) is not bool:
                raise Refusal(400, 'Поле youtube: да или нет')
            out[key] = value
        return out

    @staticmethod
    def fields(value, name, types):
        if type(value) is not dict or set(value) != set(types) or any(type(value[k]) is not t for k, t in types.items()):
            raise Refusal(400, f'Поле {name}: ожидаются {", ".join(types)}')
        return [value[key] for key in types]

    def check_exits(self, rows, config, nodevars):
        if type(rows) is not list or len(rows) > 20:
            raise Refusal(400, 'Экзиты: список до 20 строк')
        known = {row['tag']: row for row in self.draft()['exits']}
        out = []
        for row in rows:
            if type(row) is not dict or not {'tag', 'host', 'skipped'} <= set(row):
                raise Refusal(400, 'Экзит: нужны tag, host и skipped')
            tag, host, skipped = row['tag'], row['host'], row['skipped']
            if type(tag) is not str or type(host) is not str or type(skipped) is not bool:
                raise Refusal(400, 'Экзит: tag и host - строки, skipped - да или нет')
            tag, host = tag.strip(), host.strip()
            if not nodevars.TAG.fullmatch(tag) or tag in nodevars.RESERVED_TAGS or len(tag) > 63:
                raise Refusal(400, f'Тег «{tag}»: строчные латинские буквы, цифры и дефис, с буквы; не ru, all, exits, foreign')
            if tag in (item['tag'] for item in out):
                raise Refusal(400, f'Тег {tag} уже занят')
            try:
                host = config._exit_host(host, 'host')
                nodevars._check_host(host, 'host')
            except config.ConfigError as error:
                raise Refusal(400, f'Адрес {host}: {error.reason}') from None
            before = known.get(tag)
            problems = before['problems'] if before and before['host'] == host else None
            out.append({'tag': tag, 'host': host, 'skipped': skipped, 'problems': problems})
        return out

    def put_draft(self, part):
        with self.lock:
            if self.meta().get('job'):
                raise Refusal(409, 'Установка уже идёт: данные не меняются')
            draft = {**self.draft(), **self.check_draft(part)}
            self.save(draft)
            return draft

    def check_exit(self, tag):
        draft = self.draft()
        row = next((item for item in draft['exits'] if item['tag'] == tag), None)
        if not row:
            raise Refusal(400, f'Экзита {tag} нет в списке')
        problems = self.manager('exit.check', {'tag': tag, 'host': row['host']})['problems']
        with self.lock:
            draft = self.draft()
            for item in draft['exits']:
                if item['tag'] == tag and item['host'] == row['host']:
                    item['problems'] = problems
            self.save(draft)
        return problems

    # --- секреты и ключ бэкапов ---
    def secrets_set(self):
        from vpn2 import creds
        return {name: bool((HANDOVER / name).is_file() if not store else creds.read(store))
                for name, store in SECRETS.items()}

    def put_secret(self, name, value):
        from vpn2 import creds
        if name not in SECRETS or type(value) is not str or len(value) > 4096 or '\n' in value.strip():
            raise Refusal(400, 'Неизвестный секрет или значение не одной строкой')
        if self.meta().get('job'):
            raise Refusal(409, 'Установка уже идёт: данные не меняются')
        value = value.strip()
        if name == 'admin_password' and value and len(value) < 8:
            raise Refusal(400, 'Пароль администратора: не короче 8 знаков')
        if SECRETS[name]:
            creds.write(SECRETS[name], value)
        elif value:
            HANDOVER.mkdir(mode=0o700, parents=True, exist_ok=True)
            fsutil.atomic(HANDOVER / name, value, 0o600)
        else:
            (HANDOVER / name).unlink(missing_ok=True)
        return self.secrets_set()

    def backup_key(self):
        """none - пары нет, shown - выдана и ждёт подтверждения, seen - человек сохранил ключ."""
        from vpn2.backup import keys
        if (HANDOVER / 'backup.pub').is_file():
            return 'seen'
        return 'shown' if keys.recipient(STATE / 'backup') else 'none'

    def issue_key(self):
        from vpn2.backup import keys
        with self.lock:
            if self.backup_key() == 'seen':
                raise Refusal(409, 'Ключ бэкапов уже показан и подтверждён: второй раз его не показать')
            private = keys.issue(STATE / 'backup')
            return {'private_key': private, 'recipient': keys.recipient(STATE / 'backup')}

    def key_seen(self, recipient):
        from vpn2.backup import keys
        with self.lock:
            public = keys.recipient(STATE / 'backup')
            if not public or recipient != public:
                raise Refusal(409, 'Ключ бэкапов сменился (его выдали заново, например во второй вкладке): '
                                   'сохраните ключ, показанный сейчас')
            HANDOVER.mkdir(mode=0o700, parents=True, exist_ok=True)
            fsutil.atomic(HANDOVER / 'backup.pub', public + '\n', 0o600)

    # --- установка ---
    def config_text(self, draft):
        q = json.dumps
        entry = read_json(STATE / 'entry.json', {}).get('host', '')
        lines = ['# Записан мастером установки. Секреты - в хранилище entry, не здесь.',
                 'entry:', f'  host: {entry}', 'exits:']
        lines += [f'  - tag: {row["tag"]}\n    host: {q("root@" + row["host"])}' for row in draft['exits'] if not row['skipped']]
        lines += ['admin:', f'  user: {draft["panel"]["admin_user"]}', f'  ssh_pubkey: {q(draft["admin_ssh_key"])}']
        if draft['panel']['domain']:
            lines += ['panel:', f'  domain: {draft["panel"]["domain"]}']
        if draft['telegram']['enabled']:
            lines += ['telegram:', f'  chat_id: {q(draft["telegram"]["chat_id"])}']
        if draft['youtube']:
            lines += ['youtube:', '  route: zapret']
        backup, s3 = draft['backup'], draft['backup']['s3']
        lines += ['backup:', '  enabled: true', f'  backend: {backup["target"]}', '  retention: 10', '  schedule: daily']
        if backup['target'] == 's3':
            lines += ['  s3:', f'    endpoint: {q(s3["endpoint"])}', f'    bucket: {q(s3["bucket"])}']
            lines += [f'    region: {q(s3["region"])}'] if s3['region'] else []
        return '\n'.join(lines) + '\n'

    def install(self):
        with self.lock:
            draft, known = self.draft(), self.secrets_set()
            if self.meta().get('job'):
                raise Refusal(409, 'Установка уже идёт')
            rows = [row for row in draft['exits'] if not row['skipped']]
            reasons = [*([] if draft['admin_ssh_key'] else ['ключ ssh администратора (экран 1)']),
                       *(['пароль администратора (экран 2)'] if not known['admin_password'] else []),
                       *(['подтверждение, что ключ бэкапов сохранён (экран 4)'] if self.backup_key() != 'seen' else []),
                       *([f'проверка экзита {row["tag"]} (экран 1): проверьте его или продолжите без него'
                          for row in rows if row['problems'] != []])]
            if draft['backup']['target'] == 's3' and not (draft['backup']['s3']['endpoint'] and draft['backup']['s3']['bucket']
                                                         and known['s3_access_key'] and known['s3_secret_key']):
                reasons.append('адрес, бакет и ключи S3 (экран 4)')
            if draft['telegram']['enabled'] and not (draft['telegram']['chat_id'] and known['telegram_bot_token']):
                reasons.append('токен бота и чат Telegram (экран 3) - или отключите бота')
            if not rows:
                reasons.append('хотя бы один экзит с пройденной проверкой (экран 1)')
            if reasons:
                raise Refusal(409, 'Для установки не хватает: ' + '; '.join(reasons))
            fsutil.atomic(self.config, self.config_text(draft), 0o600)
            job = self.manager('install.run')
            self.set_meta(job=job['id'])
            self.installing()
            return job

    def act(self, action, tag=''):
        job = self.job()
        if not job:
            raise Refusal(409, 'Установка ещё не начиналась')
        return self.manager(f'job.{action}', {'job': job['id'], **({'tag': tag} if action == 'skip' else {})})

    def status(self):
        from vpn2 import tls
        job = self.job()
        if self.phase(job) != 'done':
            raise Refusal(409, 'Установка ещё не закончилась')
        draft, nodes = self.draft(), self.manager('status.read')['nodes']
        host = draft['panel']['domain'] or read_json(STATE / 'entry.json', {}).get('host', '')
        notice = tls.notice(read_json('/etc/vpn2/tls/state.json', {}))
        systems = [{'name': f'Экзит {node["tag"]}', 'state': 'ok', 'detail': node['host']}
                   for node in nodes if node['role'] == 'exit']
        systems += [{'name': f'Экзит {tag}', 'state': 'warning', 'detail': 'пропущен: добавить можно в настройках панели'}
                    for tag in job['skipped']]
        systems += [{'name': 'VPN: WireGuard, OpenVPN, IKEv2', 'state': 'ok', 'detail': 'проверка трафика пройдена'},
                    {'name': 'Панель', 'state': 'warning' if notice else 'ok', 'detail': notice or f'https://{host}/'},
                    {'name': 'Telegram-бот', 'state': 'ok' if draft['telegram']['enabled'] else 'warning',
                     'detail': 'включён' if draft['telegram']['enabled'] else 'не настроен: токен можно задать в панели'},
                    {'name': 'Бэкапы', 'state': 'ok', 'detail': 'ежедневно, ' + ('в S3' if draft['backup']['target'] == 's3'
                                                                                else 'на этом сервере')},
                    {'name': 'Мониторинг', 'state': 'ok' if all(node['monitoring'] == 'up' for node in nodes) else 'warning',
                     'detail': ', '.join(f'{node["tag"]}: {node["monitoring"]}' for node in nodes)}]
        if draft['youtube']:
            systems.append({'name': 'YouTube через RU', 'state': 'ok', 'detail': 'включён'})
        if not self.meta().get('status_read'):
            self.set_meta(status_read=True)
        return {'panel_url': f'https://{host}/', 'systems': systems}

    def state(self):
        from vpn2.deploy import pub, read
        job, data = self.job(), self.tokens()
        return {'phase': self.phase(job), 'draft': self.draft(), 'secrets': self.secrets_set(),
                'backup_key': self.backup_key(), 'orchestrator_key': read(pub()),
                'link_expires_at': None if self.meta().get('job') else data['expires'], 'job': job}

    # --- конец ---
    def tick(self, now):
        """После done: итог читается, пока страница открыта, но не дольше 10 минут; потом мастер уходит."""
        meta = self.meta()
        if not meta.get('job'):
            return
        if not meta.get('done_at'):
            with contextlib.suppress(Refusal):
                if self.phase(self.job()) == 'done':
                    self.set_meta(done_at=now)
            return
        if now > meta['done_at'] + DONE_LIFE or now > max(self.seen, meta['done_at']) + IDLE_AFTER_DONE:
            self.finish()

    def finish(self):
        from vpn2 import tls
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            tls.serve_routes('wizard', '')
        for path in (CADDY / 'vpn2-wizard.crt', CADDY / 'vpn2-wizard.key'):
            path.unlink(missing_ok=True)
        shutil.rmtree(RUN, ignore_errors=True)
        shutil.rmtree(STATE, ignore_errors=True)
        subprocess.run(['systemctl', 'disable', UNIT], capture_output=True)
        Path(f'/etc/systemd/system/{UNIT}.service').unlink(missing_ok=True)
        subprocess.run(['systemctl', 'stop', '--no-block', UNIT], capture_output=True)


def site(ip):
    """Маршрут мастера на сайте панели (кроме /api/version: её проверяет 99-verify) и сайт IP со своей парой - он
    действует, когда панель ушла на домен: браузер по IP остаётся на паре IP до конца установки."""
    proxy = f'reverse_proxy 127.0.0.1:{PORT} {{\n        header_up X-Forwarded-For {{remote_host}}\n    }}'
    routes = f'@wizard not path /api/version\nroute @wizard {{\n    {proxy}\n}}\n'
    own = (f'https://{ip} {{\n    tls {CADDY}/vpn2-wizard.crt {CADDY}/vpn2-wizard.key\n'
           f'    handle /api/version {{\n        import to_panel\n    }}\n    {proxy}\n}}\n')
    return routes, own


class Handler(http.server.BaseHTTPRequestHandler):
    wizard = None

    def log_message(self, *_args):
        """Журнала запросов нет: ни адресов, ни путей."""

    def reply(self, code, body, headers=()):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        for name, value in (('Content-Type', 'application/json; charset=utf-8'), ('Cache-Control', 'no-store'), *headers):
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def address(self):
        return (self.headers.get('X-Forwarded-For') or self.client_address[0]).split(',')[0].strip()

    def cookie(self):
        found = re.search(rf'(?:^|;)\s*{COOKIE}=([A-Za-z0-9_-]{{20,100}})', self.headers.get('Cookie') or '')
        return found[1] if found else ''

    def body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > 65536:
            raise Refusal(400, 'Слишком большой запрос')
        try:
            found = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            raise Refusal(400, 'Запрос не JSON') from None
        if type(found) is not dict:
            raise Refusal(400, 'Запрос не объект JSON')
        return found

    def do_GET(self):  # noqa: N802
        if self.path.startswith('/api/'):
            return self.api('GET')
        name = self.path.split('?')[0].split('#')[0].lstrip('/')
        path = (STATIC / name).resolve()
        if not name or not path.is_file() or STATIC.resolve() not in path.parents:
            path = STATIC / 'index.html'
            # Итог прочитан, страница уходит («Открыть панель» на тот же адрес): маршрут снят до ответа - браузер
            # по переадресации попадает в панель.
            if self.wizard.meta().get('status_read'):
                from vpn2 import tls
                tls.serve_routes('wizard', '')
                self.send_response(302)
                for header, value in (('Location', '/'), ('Cache-Control', 'no-store'), ('Content-Length', '0')):
                    self.send_header(header, value)
                self.end_headers()
                return self.wizard.finish()
        try:
            data = path.read_bytes()
        except OSError:
            return self.reply(503, {'refused': 'Страница мастера ещё не установлена'})
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Cache-Control', 'no-store' if path.name == 'index.html' else 'max-age=3600')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        self.api('POST')

    def do_PUT(self):  # noqa: N802
        self.api('PUT')

    def api(self, method):
        wizard, now, route = self.wizard, time.time(), self.path.split('?')[0]
        try:
            if (method, route) == ('POST', '/api/session'):
                cookie = wizard.login(self.body().get('token'), self.address(), now)
                return self.reply(200, {}, [('Set-Cookie', f'{COOKIE}={cookie}; Path=/; HttpOnly; Secure; SameSite=Strict')])
            wizard.session(self.cookie(), now)
            body = self.body() if method != 'GET' else {}
            if method == 'GET' and route == '/api/state':
                return self.reply(200, wizard.state())
            if method == 'GET' and route == '/api/job':
                return self.reply(200, {'job': wizard.job()})
            if method == 'GET' and route == '/api/status':
                return self.reply(200, wizard.status())
            if method == 'PUT' and route == '/api/draft':
                return self.reply(200, {'draft': wizard.put_draft(body)})
            if method == 'PUT' and route.startswith('/api/secret/'):
                return self.reply(200, {'secrets': wizard.put_secret(route.removeprefix('/api/secret/'), body.get('value'))})
            if method == 'POST' and route == '/api/exit/check':
                return self.reply(200, {'problems': wizard.check_exit(body.get('tag'))})
            if method == 'POST' and route == '/api/backup-key':
                return self.reply(200, wizard.issue_key())
            if method == 'POST' and route == '/api/backup-key/seen':
                wizard.key_seen(body.get('recipient'))
                return self.reply(200, {})
            if method == 'POST' and route == '/api/install':
                return self.reply(200, {'job': wizard.install()})
            if method == 'POST' and route in ('/api/job/retry', '/api/job/skip'):
                return self.reply(200, {'job': wizard.act(route.rsplit('/', 1)[1], body.get('tag', ''))})
            raise Refusal(400, 'Нет такого запроса')
        except Refusal as refusal:
            return self.reply(refusal.code, {'refused': str(refusal)})
        except (OSError, subprocess.SubprocessError) as error:
            return self.reply(500, {'refused': f'Сбой на сервере: {type(error).__name__}; повторите, при повторе - ./deploy.sh'})


def serve(port=PORT):
    """Служба мастера: HTTP на 127.0.0.1 (TLS - у Caddy) и раз в 5 с - проверка конца установки."""
    wizard = Wizard()
    Handler.wizard = wizard
    server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True

    def watch():
        while True:
            with contextlib.suppress(Exception):
                wizard.tick(time.time())
            time.sleep(5)
    threading.Thread(target=watch, daemon=True).start()
    server.serve_forever()


if __name__ == '__main__':
    serve()
