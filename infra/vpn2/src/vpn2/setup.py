"""Мастер config.yaml: задаёт вопросы, пишет config.yaml.

Запускается обёрткой setup.sh из дерева репы до установки пакета, поэтому
только стандартная библиотека. Вопросы, их порядок, проверки ввода и итоговый
файл закреплены эталоном tests/fixtures/setup.

При существующем config.yaml прежние ответы служат значениями по умолчанию:
Enter оставляет ответ как есть, в файл попадают только изменённые поля.
"""
import datetime
import ipaddress
import json
import os
import re
import secrets
import signal
import string
import subprocess
import sys
import tempfile
import termios
import unicodedata
from pathlib import Path

ROOT = Path(os.path.abspath(__file__)).parents[4]

CONTROL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
# Класс [[:space:]] локали C.UTF-8.
SPACE = (' \t\n\r\v\f\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006'
         '\u2008\u2009\u200a\u2028\u2029\u205f\u3000')
PRIVATE = re.compile(r'(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|127\.)')
TAG = re.compile(r'[a-z]([a-z0-9-]*[a-z0-9])?')
RESERVED = ('ru', 'all', 'exits', 'foreign')
BOX = 44


class Interrupted(Exception):
    """Сигнал завершения: терминал возвращается в исходный вид, процесс гибнет от сигнала."""


class Console:
    def __init__(self):
        on = os.isatty(1)
        self.B, self.G, self.Y, self.R, self.D, self.E = (
            ('\033[34m', '\033[32m', '\033[33m', '\033[31m', '\033[2m', '\033[0m') if on else ('',) * 6)

    def err(self, text):
        sys.stderr.buffer.write(text.encode('utf-8', 'surrogateescape'))
        sys.stderr.buffer.flush()

    def out(self, text):
        sys.stdout.buffer.write(text.encode('utf-8', 'surrogateescape'))
        sys.stdout.buffer.flush()

    def header(self, text):
        self.err(f'\n{self.B}  {text}{self.E}\n')

    def hint(self, text):
        self.err(f'{self.D}  {text}{self.E}\n')

    def ok(self, text):
        self.err(f'{self.G}  ✓ {text}{self.E}\n')

    def warn(self, text):
        self.err(f'{self.Y}  ! {text}{self.E}\n')

    def fail(self, text):
        self.err(f'{self.R}  ✗ {text}{self.E}\n')

    def read(self, silent=False):
        """Строка ввода без перевода строки; None - ввод кончился."""
        fd = sys.stdin.fileno()
        saved = None
        if silent and os.isatty(fd):
            saved = termios.tcgetattr(fd)
            quiet = list(saved)
            quiet[3] &= ~(termios.ECHO | termios.ECHOK | termios.ECHONL)
            termios.tcsetattr(fd, termios.TCSADRAIN, quiet)
        try:
            data = sys.stdin.buffer.readline()
        finally:
            if saved:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        if not data.endswith(b'\n'):
            return None
        return data[:-1].replace(b'\0', b'').decode('utf-8', 'surrogateescape')

    def line(self, silent=False):
        """Ответ на вопрос без своего сообщения об обрыве ввода."""
        answer = self.read(silent)
        if answer is None:
            sys.exit(1)
        return answer

    def eof(self, prompt):
        self.fail(f'EOF: {prompt} — ввод прерван')
        sys.exit(1)

    def ask(self, prompt, default=''):
        if default:
            self.err(f'  {self.Y}{prompt}{self.E} [{self.D}{default}{self.E}]: ')
        else:
            self.err(f'  {self.Y}{prompt}{self.E}: ')
        answer = self.read()
        if answer is None:
            self.eof(prompt)
        return answer or default

    def secret_prompt(self, prompt):
        self.err(f'  {self.Y}{prompt}{self.E}: ')

    def ask_ip(self, prompt, default=''):
        while True:
            value = self.ask(prompt, default)
            if default and value == default:
                return value
            try:
                ipaddress.IPv4Address(value)
                return value
            except ValueError:
                self.fail(f'Некорректный IP-адрес: {value}')

    def ask_int(self, prompt, default):
        while True:
            value = self.ask(prompt, default)
            if re.fullmatch('[1-9][0-9]*', value):
                return int(value)
            self.fail('Введите целое число ≥ 1')

    def ask_yn(self, prompt, default='n'):
        while True:
            value = self.ask(f'{prompt} (y/n)', default).lower()
            if value in ('y', 'yes'):
                return True
            if value in ('n', 'no'):
                return False
            self.fail('Введите y или n')

    def ask_password(self, prompt, stored=''):
        """Секрет без эха; Enter оставляет прежний, если он есть."""
        if stored:
            prompt += ' (Enter — оставить прежний)'
        while True:
            self.secret_prompt(prompt)
            value = self.read(silent=True)
            if value is None:
                self.eof(prompt)
            self.err('\n')
            if value or stored:
                return value or stored
            self.fail('Пароль не может быть пустым')


def sanitize(value):
    return CONTROL.sub('', value).rstrip(SPACE)


def clean(value, stored=''):
    """Прежний ответ остаётся как записан, новый чистится от управляющих символов."""
    return value if value == stored else sanitize(value)


def quoted(value):
    """Одинарные YAML-кавычки: пароль вида `x #y`, `!Qwerty1` или `12345678`
    иначе читается как комментарий, тег или число."""
    return "'" + value.replace("'", "''") + "'"


def output(*argv):
    """(успех, stdout) внешней команды; ввод мастера команде не достаётся."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return False, b''
    return done.returncode == 0, done.stdout


def field(text, marker, index):
    """Поле первой строки с marker, как awk '/marker/ {print $N}'."""
    for line in text.splitlines():
        if marker in line:
            fields = line.split()
            return fields[index] if len(fields) > index else ''
    return ''


def fetch(url):
    ok, body = output('curl', '-sSf', '--max-time', '5', url)
    return ok, body.decode('utf-8', 'surrogateescape').rstrip('\n')


def detect_entry_ip():
    address = ''
    iface = field(output('ip', '-4', 'route', 'show', 'default')[1].decode('utf-8', 'replace'),
                  'default', 4)
    if iface:
        shown = output('ip', '-4', 'addr', 'show', 'dev', iface)[1].decode('utf-8', 'replace')
        address = field(shown, 'inet ', 1).split('/')[0]
    if not address or PRIVATE.match(address):
        ok, external = fetch('https://api.ipify.org')
        if not ok:
            ok, external = fetch('https://ifconfig.me')
        if ok and external:
            address = external
    return address


def detect_geo(address):
    """(код страны строчными, название) по ip-api.com; ('', '') - не определена."""
    ok, body = output('curl', '-sSf', '--max-time', '5',
                      f'http://ip-api.com/json/{address}?fields=countryCode,country')
    if not ok:
        return '', ''
    try:
        data = json.loads(body)
        geo = data.get('countryCode', '').lower() + ':' + data.get('country', '')
    except Exception:
        return '', ''
    code, _, country = geo.rstrip('\n').partition(':')
    return code, country


SECTIONS = ('entry', 'panel', 'admin', 'alerts', 'entry_resources', 'failover', 'openvpn', 'ports',
            'sso', 'tgbot', 'wireguard', 'backup', 'telegram', 'youtube')


def checked(text, validate=True):
    """Разобранный конфиг пакетом vpn2; отказ - одной строкой с путём ключа, без значений."""
    from vpn2 import config, nodevars
    try:
        cfg = config.loads(text)
        if validate:
            nodevars.validate(cfg)
    except config.ConfigError as error:
        sys.exit(f'ERROR: {error}')
    return cfg


def parse_yaml(text):
    """Разделы config.yaml строками; файл без ключей (пустой, заготовка) - пустые разделы."""
    raw = checked(text, validate=False).raw if re.search(r'^[^#\s]', text, re.MULTILINE) else {}
    for section in SECTIONS:
        raw.setdefault(section, {})
    raw.setdefault('exits', [])
    return raw


def validate_tag(ui, tag, used):
    if not TAG.fullmatch(tag):
        ui.fail('Тег: строчная буква в начале, далее строчные буквы, цифры и дефисы; '
                'дефис в конце запрещён (пример: us, us-2)')
        return False
    if tag in RESERVED:
        ui.fail(f"Тег '{tag}' зарезервирован")
        return False
    if tag in used:
        ui.fail(f"Тег '{tag}' уже используется")
        return False
    return True


def ask_tag(ui, prompt, default, used):
    while True:
        tag = ui.ask(prompt, default)
        if validate_tag(ui, tag, used):
            return tag


def ask_exits(ui, count, stored):
    """Экзиты в порядке ввода: (тег, адрес, пароль root)."""
    exits = []
    for number in range(1, count + 1):
        ui.header(f'Выходная нода {number} / {count}')
        tag, was, password = stored[number - 1] if number <= len(stored) else ('', '', '')
        address = ui.ask_ip('IP-адрес', was)
        # Пароль и тег прежнего сервера к новому адресу не относятся.
        same = address == was
        password = ui.ask_password('root password', password if same else '')
        used = [tag for tag, _, _ in exits]
        if same and tag:
            exits.append((ask_tag(ui, 'Тег ноды', tag, used), address, password))
            continue
        ui.err(f'  {ui.D}Определяю страну...{ui.E} ')
        code, country = detect_geo(address)
        if code:
            suggested, n = code, 2
            while suggested in used:
                suggested, n = f'{code}{n}', n + 1
            ui.err(f'{ui.G}✓{ui.E} {country} ({code.upper()}) → тег «{suggested}»\n')
            tag = ask_tag(ui, 'Тег ноды', suggested, used)
        else:
            ui.err(f'{ui.Y}?{ui.E} не определена\n')
            tag = ask_tag(ui, 'Тег ноды (us / de / se / ...)', '', used)
        exits.append((tag, address, password))
    return exits


def ask_admin(ui, stored):
    """Логин, пароль ('' - сгенерирует нода) и публичный ключ ('' - без ключа)."""
    ui.header('Учётка администратора веб-панели')
    user = clean(ui.ask('Логин', stored['user']), stored['user'])
    kept = stored['password']
    if kept:
        ui.hint('Пароль: Enter — оставить прежний, иначе свой от 8 символов')
    else:
        ui.hint('Пароль: Enter — сгенерировать случайный 18-символьный, иначе свой от 8 символов')
    enter = 'оставить прежний' if kept else 'сгенерировать'
    # Короче 8 панель не примет при смене пароля, ставить такой мастером тоже нельзя.
    while True:
        ui.secret_prompt(f'Пароль (Enter — {enter})')
        password = ui.line(silent=True)
        ui.err('\n')
        if not password or len(password) >= 8:
            break
        ui.fail(f'Пароль короче 8 символов — введите длиннее или Enter, чтобы {enter}')
    if password:
        ui.secret_prompt('Пароль (повтор)')
        repeat = ui.line(silent=True)
        ui.err('\n')
        if password != repeat:
            ui.fail('Пароли не совпадают — ' + ('оставлен прежний пароль' if kept
                                               else 'пароль будет сгенерирован автоматически'))
            password = kept
    elif kept:
        password = kept
        ui.ok('Пароль оставлен прежний')
    if not password:
        ui.ok('Будет сгенерирован случайный пароль на ноде')

    ui.hint('SSH-ключ админа (опционально, но рекомендуется).')
    ui.hint('Вставь свой ПУБЛИЧНЫЙ ключ (~/.ssh/id_ed25519.pub) — он ляжет на все ноды,')
    ui.hint('а вход по паролю отключится и на entry. На экзитах пароль отключается всегда:')
    ui.hint('туда ходят по ключу оркестратора. Пусто — пароль останется только на entry')
    ui.hint('(VPN2-31: без ключа entry не запираем, иначе после переустановки не зайти).')
    kept = stored['ssh_pubkey']
    ui.secret_prompt('SSH pubkey (Enter — оставить прежний, «-» — убрать)' if kept
                     else 'SSH pubkey (Enter — пропустить)')
    # Пробелы по краям срезаются, внутри ключа они значимы.
    key = ui.line().strip(SPACE)
    if kept and key == '-':
        key = ''
    elif kept and not key:
        key = kept
        ui.ok('Ключ админа оставлен прежний')
    elif key:
        if (key.startswith(('ssh-ed25519 ', 'ssh-rsa '))
                or key.startswith('ecdsa-') and ' ' in key[6:]
                or key.startswith('sk-') and ' ' in key[3:]):
            ui.ok('Ключ принят — вход по паролю отключится на всех нодах')
        else:
            ui.fail("Не похоже на SSH-ключ (ожидается 'ssh-ed25519 AAAA...') — "
                    + ('оставлен прежний ключ' if kept else 'пропускаю'))
            key = kept
    if not key:
        ui.ok('Без ключа админа — вход по паролю останется на entry, на экзитах отключится')
    return {'user': user, 'password': password, 'ssh_pubkey': key}


def resolve(domain):
    """Последняя строка ответа dig; сбой dig читается как пустой ответ."""
    ok, body = output('dig', '+short', '+time=3', '+tries=1', domain, '@8.8.8.8')
    lines = body.decode('utf-8', 'replace').rstrip('\n').split('\n') if ok else ['']
    return lines[-1]


def ask_domain(ui, entry_ip, stored):
    ui.header('Домен для веб-панели (опционально)')
    ui.hint("Если задан — Caddy получит Let's Encrypt сертификат для https://<домен>/")
    ui.hint(f'A-запись должна указывать на IP входной ноды: {entry_ip}')
    ui.hint('Hysteria2-туннели остаются на self-signed TLS (IP), домен — только для UI')
    if not ui.ask_yn('Настроить HTTPS-домен для панели?', 'y' if stored else 'n'):
        return ''
    from vpn2.config import ConfigError
    from vpn2.nodevars import normalize_domain as normalize
    while True:
        if stored:
            domain = clean(ui.ask('Домен (например vpn.example.com)', stored), stored)
        else:
            domain = sanitize(ui.ask('Домен (например vpn.example.com; Enter — без домена)'))
        # Прежний домен уже служит панели, заново его не проверяем.
        if not domain or domain == stored:
            break
        try:
            domain = normalize(domain, 'panel.domain')
        except ConfigError:
            ui.fail('Неправильный формат домена: нужно DNS-имя без схемы, порта и пробелов')
            continue
        ui.err(f'  {ui.D}проверяю DNS...{ui.E} ')
        resolved = resolve(domain)
        if resolved == entry_ip:
            ui.err(f'{ui.G}✓{ui.E} A-запись → {resolved}\n')
            break
        if resolved:
            ui.warn(f'A-запись указывает на {resolved}, а entry IP = {entry_ip}')
            if ui.ask_yn('Продолжить?', 'n'):
                break
        else:
            ui.warn('DNS не разрешает домен (либо A-записи нет, либо нужно подождать)')
            if ui.ask_yn('Продолжить всё равно?', 'n'):
                break
    if domain:
        ui.ok(f'Домен: {domain}')
    else:
        ui.ok("Без домена — панель будет доступна по https://<IP> (Let's Encrypt на IP)")
    return domain


def ask_telegram(ui, stored):
    """Токен, chat id и ник админа; без токена или chat id бот выключен."""
    ui.header('Telegram-бот (опционально)')
    ui.hint('Уведомления: новый клиент, перегрузка CPU/RAM, метрик ноды нет или им ≥ 5 мин')
    ui.hint('По умолчанию: проверка каждые 30 с, тревога после двух проверок подряд')
    ui.hint('Нужно: создать бота через @BotFather, узнать свой Chat ID через @userinfobot')
    on = bool(stored['bot_token'] and stored['chat_id'])
    if not ui.ask_yn('Включить Telegram-бот?', 'y' if on else 'n'):
        ui.hint('Пропущено. Можно добавить позже через setup.sh')
        return dict.fromkeys(stored, '') if on else stored
    while True:
        token = clean(ui.ask_password('Токен бота (@BotFather)', stored['bot_token']),
                      stored['bot_token'])
        if token:
            break
        ui.fail('Токен не может быть пустым')
    while True:
        chat = clean(ui.ask('Chat ID (число из @userinfobot)', stored['chat_id']), stored['chat_id'])
        if chat and chat == stored['chat_id'] or re.fullmatch('-?[0-9]+', chat):
            break
        ui.fail('Chat ID — целое число (может быть отрицательным для группы)')
    ui.hint('Никнейм админа бота — его бот укажет людям без доступа («напишите @...»)')
    kept = stored['admin_username']
    if kept:
        admin = ui.ask('Никнейм админа бота (@username, опционально; «-» — убрать)', kept)
    else:
        admin = ui.ask('Никнейм админа бота (@username, опционально)')
    if admin != kept:
        admin = '' if kept and admin == '-' else sanitize(admin).removeprefix('@')
    ui.ok('Telegram-бот настроен')
    return {'bot_token': token, 'chat_id': chat, 'admin_username': admin}


def ask_backup(ui, stored):
    """Секция backup; выключенные бэкапы оставляют прежние настройки в файле.

    На чистой машине включены: Enter на всех вопросах даёт local, daily и
    сгенерированный passphrase. Вопросы S3 появляются только при выборе s3.
    Passphrase меняется только явным вводом: новым прежние архивы не открыть.
    """
    ui.header('Бэкапы БД (опционально)')
    ui.hint('Архив: db.sqlite + /etc/anysda/* + WG server key + OpenVPN CA, шифруется')
    ui.hint('age. Backend local — без внешних зависимостей; S3 — любой S3-совместимый.')
    if not ui.ask_yn('Включить бэкапы?', 'y' if stored['enabled'] else 'n'):
        ui.hint('Пропущено. Можно включить позже через setup.sh')
        return {**stored, 'enabled': False}
    was_daily = stored['schedule'] != 'off'
    daily = ui.ask_yn('Ежедневный бэкап с 02:00 (TZ entry, случайная задержка до 5 мин)?',
                      'y' if was_daily else 'n')
    schedule = stored['schedule'] if daily == was_daily else 'daily' if daily else 'off'
    while True:
        backend = clean(ui.ask('Backend (local/s3)', stored['backend']), stored['backend'])
        if backend in ('local', 's3'):
            break
        ui.fail('только local или s3')

    kept = stored['passphrase']
    if kept:
        ui.hint('Passphrase для шифрования архивов. Enter — оставить прежний.')
        ui.hint('⚠ Новый passphrase не откроет архивы, сделанные с прежним.')
        ui.secret_prompt('Backup passphrase (Enter — оставить прежний)')
    else:
        ui.hint('Passphrase для шифрования архивов. Enter — сгенерировать.')
        ui.hint('⚠ Лишишься passphrase — лишишься возможности восстановить бэкапы.')
        ui.secret_prompt('Backup passphrase (Enter — auto-gen)')
    passphrase = ui.line(silent=True) or kept
    ui.err('\n')
    if not passphrase:
        alphabet = string.ascii_letters + string.digits
        passphrase = ''.join(secrets.choice(alphabet) for _ in range(40))
        ui.warn('Passphrase сгенерирован; после сохранения он будет в config.yaml (chmod 600).')
        ui.warn('Сохраните config.yaml в защищённом месте ВНЕ entry-ноды.')
    elif kept and passphrase != kept:
        ui.warn('Passphrase изменён: архивы, сделанные раньше, открывает только прежний.')
        ui.warn('Прежний пока записан в config.yaml — сохраните его до подтверждения.')

    s3 = stored['s3']
    if backend == 's3':
        ui.hint('Endpoint любого S3-совместимого: MinIO/B2/R2/Yandex/Cloud.ru/AWS')
        while True:
            endpoint = clean(ui.ask('S3 endpoint URL', s3['endpoint']), s3['endpoint'])
            if endpoint and endpoint == s3['endpoint'] or re.match('https?://', endpoint):
                break
            ui.fail('Endpoint должен начинаться с http:// или https://')
        bucket = ''
        while not bucket:
            bucket = clean(ui.ask('S3 bucket', s3['bucket']), s3['bucket'])
        ui.hint('Region: cloud.ru→ru-central-1, yandex→ru-central1, selectel→ru-1, AWS→из endpoint')
        # Пустой region прежнего конфига остаётся пустым, а не становится us-east-1.
        was = s3['region'] or ('' if s3['endpoint'] else 'us-east-1')
        region = clean(ui.ask('S3 region', was), was)
        access = clean(ui.ask('S3 access key ID', s3['access_key']), s3['access_key'])
        secret = ui.ask_password('S3 secret access key', s3['secret_key'])
        s3 = {'endpoint': endpoint, 'bucket': bucket, 'region': region,
              'access_key': access, 'secret_key': secret}
    ui.ok(f'Бэкапы настроены (backend={backend}, schedule={schedule})')
    return {'enabled': True, 'backend': backend, 'passphrase': passphrase,
            'schedule': schedule, 's3': s3}


def ask_youtube(ui, stored):
    """YouTube не крутит рекламу на российских адресах: штатно он выходит прямо
    с entry, а DPI провайдера обходит десинк (стадия 19-yt-zapret)."""
    ui.header('YouTube (опционально)')
    ui.hint('Выпускать YouTube с entry-ноды (РФ-IP → без рекламы) + обход DPI десинком.')
    ui.hint('Иначе YouTube поедет на экзит вместе со всем остальным — с рекламой.')
    on = stored in ('zapret', 'direct')
    if ui.ask_yn('Вывести YouTube на РФ-выход entry с обходом DPI?', 'y' if on else 'n'):
        if stored == 'direct':
            ui.ok('YouTube: РФ-выход без десинка (route: direct из config.yaml оставлен)')
            return stored
        ui.ok('YouTube: РФ-выход + десинк (стадия 19-yt-zapret соберёт nfqws2 на entry)')
        return 'zapret'
    ui.hint('Пропущено. YouTube пойдёт через экзиты (с рекламой).')
    return 'off' if on else stored


def summary(ui, entry_ip, exits):
    def row(text):
        width = sum(2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in text)
        return f'{ui.B}  │{ui.E}{text}{" " * max(0, BOX - width)}{ui.B}│{ui.E}\n'

    line = '─' * BOX
    ui.out(f'{ui.B}  ┌{line}┐{ui.E}\n' + row('  Конфигурация') + f'{ui.B}  ├{line}┤{ui.E}\n'
           + row(f'  Входная нода:  {entry_ip}')
           + ''.join(row(f'  Выход {tag}: {address}') for tag, address, _ in exits)
           + f'{ui.B}  └{line}┘{ui.E}\n')


S3_KEYS = ('endpoint', 'bucket', 'region', 'access_key', 'secret_key')
NO_PASSWORD = '  # password не задан — будет сгенерирован при первом деплое'


def defaults():
    """Ответы по умолчанию на чистой машине."""
    return {'entry': {'host': '', 'password': ''}, 'exits': [],
            'admin': {'user': 'admin', 'password': '', 'ssh_pubkey': ''},
            'panel': {'domain': ''},
            'telegram': {'bot_token': '', 'chat_id': '', 'admin_username': ''},
            'backup': {'enabled': True, 'backend': 'local', 'passphrase': '', 'schedule': 'daily',
                       's3': dict.fromkeys(S3_KEYS, '')},
            'youtube': {'route': 'zapret'}}


def stored_answers(config):
    """Прежние ответы из разобранного config.yaml: то, с чем сейчас живёт деплой.

    Незаписанное поле читается так же, как его читает деплой, поэтому
    Enter на вопросе не меняет ни файл, ни поведение.
    """
    backup = config['backup']
    s3 = backup.get('s3') or {}
    answers = {name: {key: config[name].get(key, '') for key in fields}
               for name, fields in defaults().items() if name not in ('exits', 'backup')}
    answers['admin']['user'] = answers['admin']['user'] or 'anysda'
    answers['exits'] = [(node.get('tag', ''), node.get('host', ''), node.get('password', ''))
                        for node in config['exits']]
    answers['backup'] = {
        'enabled': backup.get('enabled', '').lower() in ('true', 'yes', 'y', '1'),
        'backend': backup.get('backend') or 'local', 'passphrase': backup.get('passphrase', ''),
        'schedule': backup.get('schedule') or 'daily',
        's3': {key: s3.get(key, '') for key in S3_KEYS}}
    return answers


def scalar(key, value):
    if key == 'enabled':
        return 'true' if value else 'false'
    if key == 'backend':
        return value
    if key == 'ssh_pubkey' and value:
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return quoted(value)


def exit_item(marker, node):
    """Строки экзита в списке с маркером marker («  - » у мастера, «- » у yaml.safe_dump)."""
    tag, address, secret = node
    pad = ' ' * len(marker)
    return [f'{marker}tag: {tag}', f'{pad}host: {quoted(address)}', f'{pad}password: {quoted(secret)}']


def exit_lines(exits):
    return '\n\n'.join('\n'.join(exit_item('  - ', node)) for node in exits)


def render(answers):
    """config.yaml чистой машины."""
    admin, backup = answers['admin'], answers['backup']
    lines = ['# anysda-vpn — конфигурация (chmod 600 — содержит пароли)',
             '# Создано ./setup.sh ' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
             '', 'entry:', *(f'  {key}: {quoted(value)}' for key, value in answers['entry'].items()),
             '', 'exits:', exit_lines(answers['exits']),
             '', 'admin:', f"  user: {quoted(admin['user'])}",
             f"  password: {quoted(admin['password'])}"]
    if not admin['password']:
        lines.append(NO_PASSWORD)
    lines += [f"  ssh_pubkey: {scalar('ssh_pubkey', admin['ssh_pubkey'])}",
              '', 'panel:', f"  domain: {quoted(answers['panel']['domain'])}",
              '', 'telegram:',
              *(f'  {key}: {quoted(value)}' for key, value in answers['telegram'].items()),
              '', 'backup:', f"  enabled: {scalar('enabled', backup['enabled'])}"]
    if backup['enabled']:
        lines += [f'  {key}: {scalar(key, backup[key])}' for key in ('backend', 'passphrase', 'schedule')]
        if backup['backend'] == 's3':
            lines += ['  s3:', *(f'    {key}: {quoted(backup["s3"][key])}' for key in S3_KEYS)]
    # quic и retention не спрашиваются: значения по умолчанию берёт деплой.
    lines += ['', 'youtube:', f"  route: {quoted(answers['youtube']['route'])}"]
    return '\n'.join(lines) + '\n'


def changes(old, new, everything=False):
    """Ответы, отличные от прежних: {секция: {ключ: значение YAML}}; s3 - словарь своих полей, exits - список.

    Неизменённое в файл не пишется: так Enter на всех вопросах оставляет
    config.yaml байт в байт, а правка одного ответа меняет одну строку.
    """
    result = {}
    for name in new:
        if name == 'exits':
            if everything or new['exits'] != old['exits']:
                result['exits'] = new['exits']
            continue
        fields = {key: scalar(key, value) for key, value in new[name].items()
                  if key != 's3' and (everything or value != old[name][key])}
        if name == 'backup' and everything and not new[name]['enabled']:
            fields = {'enabled': fields['enabled']}   # как на чистой машине: выключенным бэкапам хватает enabled
        elif name == 'backup':
            s3 = {key: quoted(value) for key, value in new[name]['s3'].items() if value != old[name]['s3'][key]}
            if s3:
                fields['s3'] = s3
        if fields:
            result[name] = fields
    return result


def _key(line, indent):
    """Имя поля YAML на отступе indent или None."""
    found = re.match(' ' * indent + r'([^ #\n-][^:\n]*):', line)
    return found[1].strip() if found else None


def _block_end(lines, start, indent):
    """Конец блока полей на отступе indent: первая строка от start с полем левее."""
    outer = re.compile(r' {0,%d}[^ #\n-][^:\n]*:' % (indent - 2))
    return next((i for i in range(start, len(lines)) if outer.match(lines[i])), len(lines))


def _meaningful_end(lines, start, end):
    """Пустые строки и комментарии в конце блока остаются за его полями."""
    while end > start and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith('#')):
        end -= 1
    return end


def _without_blank_tail(item):
    while not item[-1].strip():
        item = item[:-1]
    return item


def _field_lines(fields, indent):
    lines = []
    for key, value in fields.items():
        if isinstance(value, dict):
            lines += [' ' * indent + f'{key}:', *_field_lines(value, indent + 2)]
        else:
            lines.append(' ' * indent + f'{key}: {value}')
    return lines


def _keep_comment(old, new):
    """Новая строка поля с комментарием прежней; # внутри кавычек - часть значения."""
    value = old.partition(':')[2].lstrip()
    if value.startswith('#'):
        return f'{new} {value}'
    return new + (re.fullmatch(r'''(?:'(?:[^']|'')*'|"(?:[^"\\]|\\.)*"|.*?)([ \t]+\#.*)?''', value)[1] or '')


def _put(lines, start, indent, fields):
    """Поля в блок от строки start: прежняя строка поля меняется, новое поле встаёт за последним."""
    for key, value in fields.items():
        end = _block_end(lines, start, indent)
        at = next((i for i in range(start, end) if _key(lines[i], indent) == key), None)
        if at is not None and isinstance(value, dict):
            _put(lines, at + 1, indent + 2, value)
        elif at is not None:
            lines[at] = _keep_comment(lines[at], ' ' * indent + f'{key}: {value}')
        else:
            end = _meaningful_end(lines, start, end)
            lines[end:end] = _field_lines({key: value}, indent)


def _put_exits(lines, start, stored, exits):
    """Экзиты по тегу: у оставшихся меняются host и password, прочие их строки целы; убранные уходят."""
    tags = [node.get('tag') for node in stored]
    if any(not tag for tag in tags) or len(tags) != len(set(tags)):
        sys.exit('ERROR: config.yaml: exits: для сохранения настроек нужны уникальные непустые теги')
    end = _meaningful_end(lines, start, _block_end(lines, start, 2))
    starts = [i for i in range(start, end) if re.match(' *- ', lines[i])]
    marker = re.match(' *- ', lines[starts[0]])[0] if starts else '  - '
    items = [_without_blank_tail(lines[first:last]) for first, last in zip(starts, [*starts[1:], end])]
    # Пустая строка между экзитами - как в прежнем файле; у одного экзита - как пишет мастер.
    gap = [''] if len(items) < 2 or len(items[0]) < starts[1] - starts[0] else []
    by_tag = dict(zip(tags, items))
    merged = []
    for node in exits:
        item = list(by_tag.get(node[0]) or exit_item(marker, node))
        if node[0] in by_tag:
            item[0] = ' ' * len(marker) + item[0][len(marker):]
            _put(item, 0, len(marker), {'host': quoted(node[1]), 'password': quoted(node[2])})
            item[0] = marker + item[0][len(marker):]
        merged += [*(gap if merged else []), *item]
    lines[starts[0] if starts else end:end] = merged


def update(previous, changed):
    """Прежний config.yaml с изменёнными ответами; остальные строки и комментарии не тронуты.

    Новое поле встаёт за последним полем своей секции, секция без раздела в файле дописывается в конец.
    """
    lines = previous.rstrip().split('\n')
    appended = []
    for name, fields in changed.items():
        at = next((i for i, line in enumerate(lines) if _key(line, 0) == name), None)
        if at is None:
            body = exit_lines(fields) if name == 'exits' else '\n'.join(_field_lines(fields, 2))
            appended.append(f'{name}:\n{body}\n')
        elif name == 'exits':
            _put_exits(lines, at + 1, parse_yaml(previous)['exits'], fields)
        else:
            _put(lines, at + 1, 2, fields)
    if changed.get('admin', {}).get('password', "''") != "''" and NO_PASSWORD in lines:
        lines.remove(NO_PASSWORD)
    return '\n'.join(lines) + '\n' + ''.join('\n' + section for section in appended)


def save(text):
    """Замена атомарна, файл сразу 0600: прежний config.yaml цел до последнего шага."""
    config = ROOT / 'config.yaml'
    fd, temporary = tempfile.mkstemp(prefix='.config-', suffix='.yaml', dir=config.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(text)
        os.replace(temporary, config)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    config.chmod(0o600)


def wizard(ui):
    config = ROOT / 'config.yaml'
    previous = config.read_text(encoding='utf-8') if config.exists() else None
    # Битый config.yaml останавливает мастер до вопросов: править его вслепую нельзя.
    known = stored_answers(parse_yaml(previous)) if previous is not None else None
    # Файл без нод (пустой или заготовка) - ещё не конфигурация: вопросы как на чистой машине.
    rerun = bool(known and (known['entry']['host'] or known['exits']))
    old = known if rerun else defaults()

    ui.err('\n'
           f'{ui.B}  ┌────────────────────────────────────────────┐{ui.E}\n'
           f'{ui.B}  │{ui.E}  anysda-vpn  ·  настройка инфраструктуры   {ui.B}│{ui.E}\n'
           f'{ui.B}  └────────────────────────────────────────────┘{ui.E}\n')
    ui.hint('Работает по IP — домены не нужны')
    ui.hint('Деплой идёт от root по SSH-паролю (config.yaml будет chmod 600)')
    ui.hint('Повторный ./setup.sh обновляет только спрошенные поля; остальные настройки '
            'и комментарии в config.yaml сохраняются')
    if rerun:
        ui.hint('Найден config.yaml: Enter на вопросе оставляет прежний ответ, '
                'пароли и passphrase сами не меняются')

    ui.header('Сколько выходных серверов?')
    ui.hint('Минимум 1. Каждый даёт отдельный Hysteria2 endpoint.')
    count = ui.ask_int('Количество', str(len(old['exits']) or 1))

    ui.header('Входная нода (entry) — WireGuard endpoint + панель управления')
    detected, entry_ip = detect_entry_ip(), old['entry']['host']
    if entry_ip and detected and detected != entry_ip:
        # Записанный адрес уже служит деплою: молча его не меняем.
        ui.warn(f'Автоопределение даёт {detected}, в config.yaml записан {entry_ip}')
        entry_ip = ui.ask_ip('IP-адрес входной ноды', entry_ip)
    elif entry_ip or detected:
        entry_ip = entry_ip or detected
        ui.ok(f'IP входной ноды: {entry_ip}')
    else:
        ui.fail('Не удалось автоопределить IP. Введите вручную.')
        entry_ip = ui.ask_ip('IP-адрес входной ноды')
    new = {'entry': {'host': entry_ip,
                     'password': ui.ask_password('root password', old['entry']['password'])}}

    new['exits'] = ask_exits(ui, count, old['exits'])
    new['admin'] = ask_admin(ui, old['admin'])
    new['panel'] = {'domain': ask_domain(ui, entry_ip, old['panel']['domain'])}
    new['telegram'] = ask_telegram(ui, old['telegram'])
    new['backup'] = ask_backup(ui, old['backup'])
    new['youtube'] = {'route': ask_youtube(ui, old['youtube']['route'])}

    ui.err('\n')
    summary(ui, entry_ip, new['exits'])
    ui.err('\n')
    if not ui.ask_yn('Сохранить config.yaml?', 'y'):
        ui.err('  Отменено.\n')
        return 0
    if previous is None:
        save(render(new))
    elif config.read_text(encoding='utf-8') != previous:
        ui.fail('config.yaml изменился, пока шли вопросы — запустите ./setup.sh заново')
        return 1
    else:
        changed = changes(known, new, everything=not rerun)
        if changed:
            text = update(previous, changed)
            checked(text)
            save(text)
        else:
            config.chmod(0o600)
            ui.ok('config.yaml без изменений')

    ui.err('\n')
    code = subprocess.run([sys.executable, str(ROOT / 'infra/ansible/inventory.py'), '--prepare'], check=False,
                          env={**os.environ, 'PYTHONPATH': str(ROOT / 'infra/vpn2/src')}).returncode
    if code:
        return code
    ui.err('\n')
    ui.ok('Готово!')
    ui.err(f'\n\n  Следующий шаг:  {ui.G}./deploy.sh{ui.E}\n\n')
    return 0


def interrupt(number, _frame):
    raise Interrupted(number)


def main():
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, interrupt)
    try:
        return wizard(Console())
    except Interrupted as stop:
        # Режим терминала уже возвращён; config.yaml не тронут или заменён целиком.
        signal.signal(stop.args[0], signal.SIG_DFL)
        os.kill(os.getpid(), stop.args[0])
        return 1


if __name__ == '__main__':
    sys.exit(main())
