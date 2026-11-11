"""Мастер config.yaml: задаёт вопросы, пишет config.yaml, генерирует infra/envs.

Запускается обёрткой setup.sh из дерева репы до установки пакета, поэтому
только стандартная библиотека. Вопросы, их порядок, проверки ввода и итоговый
файл закреплены эталоном tests/fixtures/setup.
"""
import datetime
import ipaddress
import json
import os
import re
import runpy
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

    def ask_ip(self, prompt):
        while True:
            value = self.ask(prompt)
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

    def ask_password(self, prompt):
        while True:
            self.secret_prompt(prompt)
            value = self.read(silent=True)
            if value is None:
                self.eof(prompt)
            self.err('\n')
            if value:
                return value
            self.fail('Пароль не может быть пустым')


def sanitize(value):
    return CONTROL.sub('', value).rstrip(SPACE)


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


def config2env():
    return runpy.run_path(str(ROOT / 'infra/lib/config2env.py'))


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


def ask_exits(ui, count):
    """Экзиты в порядке ввода: (тег, адрес, пароль root)."""
    exits = []
    for number in range(1, count + 1):
        ui.header(f'Выходная нода {number} / {count}')
        address = ui.ask_ip('IP-адрес')
        password = ui.ask_password('root password')
        ui.err(f'  {ui.D}Определяю страну...{ui.E} ')
        code, country = detect_geo(address)
        used = [tag for tag, _, _ in exits]
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


def ask_admin(ui):
    """(логин, пароль или '', публичный ключ или '')."""
    ui.header('Учётка администратора веб-панели')
    user = sanitize(ui.ask('Логин', 'admin'))
    ui.hint('Пароль: Enter — сгенерировать случайный 18-символьный, иначе свой от 8 символов')
    # Короче 8 панель не примет при смене пароля, ставить такой мастером тоже нельзя.
    while True:
        ui.secret_prompt('Пароль (Enter — сгенерировать)')
        password = ui.line(silent=True)
        ui.err('\n')
        if not password or len(password) >= 8:
            break
        ui.fail('Пароль короче 8 символов — введите длиннее или Enter, чтобы сгенерировать')
    if password:
        ui.secret_prompt('Пароль (повтор)')
        repeat = ui.line(silent=True)
        ui.err('\n')
        if password != repeat:
            ui.fail('Пароли не совпадают — пароль будет сгенерирован автоматически')
            password = ''
    if not password:
        ui.ok('Будет сгенерирован случайный пароль на ноде')

    ui.hint('SSH-ключ админа (опционально, но рекомендуется).')
    ui.hint('Вставь свой ПУБЛИЧНЫЙ ключ (~/.ssh/id_ed25519.pub) — он ляжет на все ноды,')
    ui.hint('а вход по паролю отключится и на entry. На экзитах пароль отключается всегда:')
    ui.hint('туда ходят по ключу оркестратора. Пусто — пароль останется только на entry')
    ui.hint('(VPN2-31: без ключа entry не запираем, иначе после переустановки не зайти).')
    ui.secret_prompt('SSH pubkey (Enter — пропустить)')
    # Пробелы по краям срезаются, внутри ключа они значимы.
    key = ui.line().strip(SPACE)
    if key:
        if (key.startswith(('ssh-ed25519 ', 'ssh-rsa '))
                or key.startswith('ecdsa-') and ' ' in key[6:]
                or key.startswith('sk-') and ' ' in key[3:]):
            ui.ok('Ключ принят — вход по паролю отключится на всех нодах')
        else:
            ui.fail("Не похоже на SSH-ключ (ожидается 'ssh-ed25519 AAAA...') — пропускаю")
            key = ''
    if not key:
        ui.ok('Без ключа админа — вход по паролю останется на entry, на экзитах отключится')
    return user, password, key


def resolve(domain):
    """Последняя строка ответа dig; сбой dig читается как пустой ответ."""
    ok, body = output('dig', '+short', '+time=3', '+tries=1', domain, '@8.8.8.8')
    lines = body.decode('utf-8', 'replace').rstrip('\n').split('\n') if ok else ['']
    return lines[-1]


def ask_domain(ui, entry_ip):
    ui.header('Домен для веб-панели (опционально)')
    ui.hint("Если задан — Caddy получит Let's Encrypt сертификат для https://<домен>/")
    ui.hint(f'A-запись должна указывать на IP входной ноды: {entry_ip}')
    ui.hint('Hysteria2-туннели остаются на self-signed TLS (IP), домен — только для UI')
    if not ui.ask_yn('Настроить HTTPS-домен для панели?', 'n'):
        return ''
    normalize = config2env()['normalize_domain']
    while True:
        domain = sanitize(ui.ask('Домен (например vpn.example.com; Enter — без домена)'))
        if not domain:
            break
        try:
            domain = normalize(domain, 'panel.domain')
        except SystemExit:
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


def ask_telegram(ui):
    """(токен, chat id, ник админа); пустые строки - бот выключен."""
    ui.header('Telegram-бот (опционально)')
    ui.hint('Уведомления: новый клиент, перегрузка CPU/RAM, метрик ноды нет или им ≥ 5 мин')
    ui.hint('По умолчанию: проверка каждые 30 с, тревога после двух проверок подряд')
    ui.hint('Нужно: создать бота через @BotFather, узнать свой Chat ID через @userinfobot')
    if not ui.ask_yn('Включить Telegram-бот?', 'n'):
        ui.hint('Пропущено. Можно добавить позже через setup.sh')
        return '', '', ''
    while True:
        token = sanitize(ui.ask_password('Токен бота (@BotFather)'))
        if token:
            break
        ui.fail('Токен не может быть пустым')
    while True:
        chat = sanitize(ui.ask('Chat ID (число из @userinfobot)'))
        if re.fullmatch('-?[0-9]+', chat):
            break
        ui.fail('Chat ID — целое число (может быть отрицательным для группы)')
    ui.hint('Никнейм админа бота — его бот укажет людям без доступа («напишите @...»)')
    admin = sanitize(ui.ask('Никнейм админа бота (@username, опционально)'))
    ui.ok('Telegram-бот настроен')
    return token, chat, admin.removeprefix('@')


def ask_backup(ui):
    """Строки секции backup после `enabled`; None - бэкапы выключены.

    Включены по умолчанию: Enter на всех вопросах даёт local, daily и
    сгенерированный passphrase. Вопросы S3 появляются только при выборе s3.
    """
    ui.header('Бэкапы БД (опционально)')
    ui.hint('Архив: db.sqlite + /etc/anysda/* + WG server key + OpenVPN CA, шифруется')
    ui.hint('age. Backend local — без внешних зависимостей; S3 — любой S3-совместимый.')
    if not ui.ask_yn('Включить бэкапы?', 'y'):
        ui.hint('Пропущено. Можно включить позже через setup.sh')
        return None
    daily = ui.ask_yn('Ежедневный бэкап с 02:00 (TZ entry, случайная задержка до 5 мин)?', 'y')
    schedule = 'daily' if daily else 'off'
    while True:
        backend = sanitize(ui.ask('Backend (local/s3)', 'local'))
        if backend in ('local', 's3'):
            break
        ui.fail('только local или s3')

    ui.hint('Passphrase для шифрования архивов. Enter — сгенерировать.')
    ui.hint('⚠ Лишишься passphrase — лишишься возможности восстановить бэкапы.')
    ui.secret_prompt('Backup passphrase (Enter — auto-gen)')
    passphrase = ui.line(silent=True)
    ui.err('\n')
    if not passphrase:
        alphabet = string.ascii_letters + string.digits
        passphrase = ''.join(secrets.choice(alphabet) for _ in range(40))
        ui.warn('Passphrase сгенерирован; после сохранения он будет в config.yaml (chmod 600).')
        ui.warn('Сохраните config.yaml в защищённом месте ВНЕ entry-ноды.')

    lines = [f'  backend: {backend}', f'  passphrase: {quoted(passphrase)}',
             f'  schedule: {quoted(schedule)}']
    if backend == 's3':
        ui.hint('Endpoint любого S3-совместимого: MinIO/B2/R2/Yandex/Cloud.ru/AWS')
        while True:
            endpoint = sanitize(ui.ask('S3 endpoint URL'))
            if re.match('https?://', endpoint):
                break
            ui.fail('Endpoint должен начинаться с http:// или https://')
        bucket = ''
        while not bucket:
            bucket = sanitize(ui.ask('S3 bucket'))
        ui.hint('Region: cloud.ru→ru-central-1, yandex→ru-central1, selectel→ru-1, AWS→из endpoint')
        region = sanitize(ui.ask('S3 region', 'us-east-1'))
        access = sanitize(ui.ask('S3 access key ID'))
        secret = ui.ask_password('S3 secret access key')
        lines += ['  s3:', f'    endpoint: {quoted(endpoint)}', f'    bucket: {quoted(bucket)}',
                  f'    region: {quoted(region)}', f'    access_key: {quoted(access)}',
                  f'    secret_key: {quoted(secret)}']
    ui.ok(f'Бэкапы настроены (backend={backend}, schedule={schedule})')
    return lines


def ask_youtube(ui):
    """YouTube не крутит рекламу на российских адресах: штатно он выходит прямо
    с entry, а DPI провайдера обходит десинк (стадия 19-yt-zapret)."""
    ui.header('YouTube (опционально)')
    ui.hint('Выпускать YouTube с entry-ноды (РФ-IP → без рекламы) + обход DPI десинком.')
    ui.hint('Иначе YouTube поедет на экзит вместе со всем остальным — с рекламой.')
    if ui.ask_yn('Вывести YouTube на РФ-выход entry с обходом DPI?', 'y'):
        ui.ok('YouTube: РФ-выход + десинк (стадия 19-yt-zapret соберёт nfqws2 на entry)')
        return 'zapret'
    ui.hint('Пропущено. YouTube пойдёт через экзиты (с рекламой).')
    return 'off'


def summary(ui, entry_ip, exits):
    def row(text):
        width = sum(2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in text)
        return f'{ui.B}  │{ui.E}{text}{" " * max(0, BOX - width)}{ui.B}│{ui.E}\n'

    line = '─' * BOX
    ui.out(f'{ui.B}  ┌{line}┐{ui.E}\n' + row('  Конфигурация') + f'{ui.B}  ├{line}┤{ui.E}\n'
           + row(f'  Входная нода:  {entry_ip}')
           + ''.join(row(f'  Выход {tag}: {address}') for tag, address, _ in exits)
           + f'{ui.B}  └{line}┘{ui.E}\n')


def render(entry_ip, entry_password, exits, admin, domain, telegram, backup, youtube):
    user, password, key = admin
    lines = ['# anysda-vpn — конфигурация (chmod 600 — содержит пароли)',
             '# Создано ./setup.sh ' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
             '', 'entry:', f'  host: {quoted(entry_ip)}', f'  password: {quoted(entry_password)}',
             '', 'exits:']
    lines.append('\n\n'.join(
        f'  - tag: {tag}\n    host: {quoted(address)}\n    password: {quoted(secret)}'
        for tag, address, secret in exits))
    lines += ['', 'admin:', f'  user: {quoted(user)}']
    if password:
        lines.append(f'  password: {quoted(password)}')
    else:
        lines += ["  password: ''", '  # password не задан — будет сгенерирован при первом деплое']
    key = key.strip()
    if key:
        lines.append('  ssh_pubkey: "' + key.replace('\\', '\\\\').replace('"', '\\"') + '"')
    else:
        lines.append("  ssh_pubkey: ''")
    token, chat, username = telegram
    lines += ['', 'panel:', f'  domain: {quoted(domain)}',
              '', 'telegram:', f'  bot_token: {quoted(token)}', f'  chat_id: {quoted(chat)}',
              f'  admin_username: {quoted(username)}']
    # Явное false перекрывает старый ответ, сохраняя незаданные настройки.
    lines += ['', 'backup:', '  enabled: ' + ('false' if backup is None else 'true'), *(backup or [])]
    # quic и retention не спрашиваются: существующие значения сохраняются,
    # для нового конфига значения по умолчанию берёт config2env.
    lines += ['', 'youtube:', f'  route: {quoted(youtube)}']
    return '\n'.join(lines) + '\n'


def save(text):
    """Разбор и слияние завершаются до записи; замена атомарна, файл сразу 0600."""
    config = ROOT / 'config.yaml'
    if config.exists():
        text = config2env()['merge_setup_config'](config.read_text(encoding='utf-8'), text)
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
    ui.err('\n'
           f'{ui.B}  ┌────────────────────────────────────────────┐{ui.E}\n'
           f'{ui.B}  │{ui.E}  anysda-vpn  ·  настройка инфраструктуры   {ui.B}│{ui.E}\n'
           f'{ui.B}  └────────────────────────────────────────────┘{ui.E}\n')
    ui.hint('Работает по IP — домены не нужны')
    ui.hint('Деплой идёт от root по SSH-паролю (config.yaml будет chmod 600)')
    ui.hint('Повторный ./setup.sh обновляет только спрошенные поля; остальные настройки '
            'и комментарии в config.yaml сохраняются')

    ui.header('Сколько выходных серверов?')
    ui.hint('Минимум 1. Каждый даёт отдельный Hysteria2 endpoint.')
    count = ui.ask_int('Количество', '1')

    ui.header('Входная нода (entry) — WireGuard endpoint + панель управления')
    entry_ip = detect_entry_ip()
    if entry_ip:
        ui.ok(f'IP входной ноды: {entry_ip}')
    else:
        ui.fail('Не удалось автоопределить IP. Введите вручную.')
        entry_ip = ui.ask_ip('IP-адрес входной ноды')
    entry_password = ui.ask_password('root password')

    exits = ask_exits(ui, count)
    admin = ask_admin(ui)
    domain = ask_domain(ui, entry_ip)
    telegram = ask_telegram(ui)
    backup = ask_backup(ui)
    youtube = ask_youtube(ui)

    ui.err('\n')
    summary(ui, entry_ip, exits)
    ui.err('\n')
    if not ui.ask_yn('Сохранить config.yaml и сгенерировать envs?', 'y'):
        ui.err('  Отменено.\n')
        return 0
    save(render(entry_ip, entry_password, exits, admin, domain, telegram, backup, youtube))

    ui.err('\n')
    code = subprocess.run([sys.executable, str(ROOT / 'infra/lib/config2env.py'), str(ROOT)],
                          check=False).returncode
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
