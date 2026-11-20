"""Секреты entry: зашифрованное хранилище systemd-creds с ключом самого сервера и виды на tmpfs.

Хранилище - STORE/<имя>.cred, только root; копия на другой машине не расшифровывается. Службы берут
секреты при старте из видов в /run/vpn2-secrets: root/ - все, panel/ и bot/ - нужные контейнеру.
Виды собирает publish(): при загрузке (vpn2-secrets.service до sysinit.target) и после каждой записи.
"""
import contextlib
import grp
import hashlib
import json
import os
import secrets
import stat
import string
import subprocess
import sys
from pathlib import Path

from vpn2 import fsutil

STORE = Path(os.environ.get('VPN2_CREDS_STORE') or '/var/lib/vpn2-secrets')
VIEWS = Path(os.environ.get('VPN2_CREDS_VIEWS') or '/run/vpn2-secrets')
NAMES = ('telegram_bot_token', 's3_access_key', 's3_secret_key', 'sso_client_secret', 'backup_passphrase',
         'orchestrator_key', 'mgmt_key', 'session_secret', 'tgbot_secret', 'adguard_password')
# Виды контейнеров: группа каталога и имена. clash_secret - копия секрета из конфига sing-box.
SHARED = {'panel': ('vpn2-panel', ('session_secret', 'tgbot_secret', 'adguard_password', 'sso_client_secret',
                                   'telegram_bot_token', 'clash_secret')),
          'bot': ('vpn2-bot', ('telegram_bot_token', 'tgbot_secret'))}
CLASH = Path('/etc/anysda/clash-secret.txt')
# Секреты, которые задают в панели: она кладёт файл в inbox/, юнит vpn2-secrets-inbox переносит его в хранилище.
INBOX = ('telegram_bot_token', 'sso_client_secret')
# Ключи ssh самой entry: в бэкап не идут, restore их не меняет - ими эта entry ходит на экзиты.
MACHINE = ('orchestrator_key', 'mgmt_key')
# Места 1.2.0: секрет переезжает в хранилище, файл удаляется. Пароль админа не переезжает никуда.
LEGACY_FILES = {'session_secret': '/etc/anysda/session-secret.txt', 'tgbot_secret': '/etc/anysda/tgbot-secret.txt',
                'orchestrator_key': '~/.ssh/id_ed25519', 'mgmt_key': '~/.ssh/id_ed25519_mgmt'}
LEGACY_DROP = ('/etc/anysda/admin-password.txt', '/etc/anysda/adguard-password.txt',
               '/var/lib/anysda-vpn2/admin-password.txt')
LEGACY_JSON = (('/var/lib/anysda-vpn2/runtime/telegram-runtime.json', 'bot_token', 'telegram_bot_token'),
               ('/var/lib/anysda-vpn2/runtime/sso-runtime.json', 'clientSecret', 'sso_client_secret'),
               # Токен, который 1.2.0 взял из config.yaml: только стирается, действовал токен из панели.
               ('/etc/anysda/telegram-from-config.json', 'bot_token', None))


class CredsError(Exception):
    pass


def _creds(*args, data=None):
    done = subprocess.run(['systemd-creds', *args], input=data, capture_output=True, check=False)
    if done.returncode:
        detail = done.stderr.decode(errors='replace').strip().splitlines()[-1:] or ['']
        raise CredsError(f'systemd-creds {args[0]}: код {done.returncode} {detail[0][:200]}')
    return done.stdout


def _path(name):
    return STORE / f'{name}.cred'


def _decrypt(name):
    return _creds('decrypt', f'--name={name}', str(_path(name)), '-').decode()


def read(name):
    """Значение секрета; нет его - пустая строка. Вид root/ на tmpfs - тот же секрет без расшифровки."""
    with contextlib.suppress(OSError):
        return (VIEWS / 'root' / name).read_text()
    return _decrypt(name) if _path(name).is_file() else ''


def write(name, value):
    """Секрет в хранилище и в виды; пустое value снимает его. Вернуть, сменился ли он."""
    if name not in NAMES:
        raise CredsError(f'неизвестный секрет {name}')
    STORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with fsutil.lock(STORE / '.lock'):
        if value == (_decrypt(name) if _path(name).is_file() else ''):
            return False
        if value:
            fsutil.atomic(_path(name), _creds('encrypt', '--with-key=host', f'--name={name}', '-', '-',
                                              data=value.encode()))
        else:
            _path(name).unlink(missing_ok=True)
        publish()
    return True


def ensure(name, length=32):
    """Секрет, который создаётся один раз: есть - прежний, нет - случайный."""
    value = read(name)
    if not value:
        value = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))
        write(name, value)
    return value


def _put(folder, name, value, mode, gid):
    """Файл вида: права и группа - до подмены, читатель не видит чужих прав."""
    temporary = folder / f'.{name}.tmp'
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, 'w') as out:
        if gid is not None:
            os.fchown(out.fileno(), -1, gid)
        os.fchmod(out.fileno(), mode)
        out.write(value)
    os.replace(temporary, folder / name)


def publish():
    """Виды из хранилища; вид контейнера без его группы на узле не собирается."""
    values = {name: _decrypt(name) for name in NAMES if _path(name).is_file()}
    with contextlib.suppress(OSError):
        values['clash_secret'] = CLASH.read_text().strip()
    VIEWS.mkdir(mode=0o755, parents=True, exist_ok=True)
    views = {'root': (None, NAMES), **SHARED}
    for view, (group, names) in views.items():
        try:
            gid = grp.getgrnam(group).gr_gid if group else None
        except KeyError:
            continue
        folder = VIEWS / view
        folder.mkdir(exist_ok=True)
        if gid is not None:
            os.chown(folder, -1, gid)
        folder.chmod(0o750 if group else 0o700)
        for name in names:
            if name in values:
                _put(folder, name, values[name], 0o640 if group else 0o600, gid)
            else:
                (folder / name).unlink(missing_ok=True)
        if view == 'panel':
            (VIEWS / 'inbox').mkdir(exist_ok=True)
            os.chown(VIEWS / 'inbox', -1, gid)
            (VIEWS / 'inbox').chmod(0o770)


def take_inbox():
    """Секреты из inbox/ панели - в хранилище: только обычный файл с именем из INBOX; всё прочее удаляется."""
    folder = VIEWS / 'inbox'
    for item in sorted(folder.iterdir()) if folder.is_dir() else ():
        if item.name.startswith('.'):
            continue
        with contextlib.suppress(OSError):
            descriptor = os.open(item, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as handle:
                info = os.fstat(handle.fileno())
                if item.name in INBOX and stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 4096:
                    write(item.name, handle.read().decode(errors='replace').strip())
        with contextlib.suppress(OSError):
            item.unlink()


def key_file(name):
    """Путь ключа ssh в виде root/: после перезагрузки до первой службы вид собирается заново."""
    path = VIEWS / 'root' / name
    if not path.is_file() and _path(name).is_file():
        publish()
    return path


def digest(view):
    """Отпечаток вида контейнера: сменился секрет - контейнер пересоздаётся."""
    folder, text = VIEWS / view, b''
    for name in SHARED[view][1]:
        with contextlib.suppress(OSError):
            text += name.encode() + b'\0' + (folder / name).read_bytes() + b'\0'
    return hashlib.sha256(text).hexdigest()


def _rewrite_json(path, data):
    stat = os.stat(path)
    temporary = f'{path}.creds'
    with open(temporary, 'w', encoding='utf-8') as out:
        os.fchown(out.fileno(), stat.st_uid, stat.st_gid)
        os.fchmod(out.fileno(), stat.st_mode & 0o7777)
        out.write(json.dumps(data) + '\n')
    os.replace(temporary, path)


def adopt(root='/'):
    """Секреты из мест 1.2.0 под root - в хранилище, места очищаются; повтор после обрыва доводит перенос.

    Секрет, заданный в панели (telegram-runtime.json, sso-runtime.json), сильнее прежнего в хранилище.
    """
    def place(file):
        return os.path.join(root, os.path.expanduser(file).lstrip('/'))

    for name, file in ((name, place(file)) for name, file in LEGACY_FILES.items()):
        with contextlib.suppress(FileNotFoundError):
            value = Path(file).read_text()
            if value.strip():
                write(name, value if name.endswith('_key') else value.strip())
            os.unlink(file)
    for file, field, name in ((place(file), field, name) for file, field, name in LEGACY_JSON):
        with contextlib.suppress(FileNotFoundError, ValueError):
            data = json.loads(Path(file).read_text())
            if isinstance(data, dict) and field in data:
                if name and str(data[field] or '').strip():
                    write(name, str(data[field]).strip())
                del data[field]
                _rewrite_json(file, data)
    for file in map(place, LEGACY_DROP):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(file)


def bundle(target):
    """Секреты открытым JSON в набор бэкапа: архив целиком зашифрован, restore шифрует их ключом нового сервера."""
    values = {name: read(name) for name in NAMES if name not in MACHINE}
    fsutil.write_json(target, {name: value for name, value in values.items() if value})


def unbundle(source):
    """Секреты из набора бэкапа - в хранилище этого сервера."""
    for name, value in fsutil.read_json(source).items():
        if name in NAMES and name not in MACHINE and isinstance(value, str):
            write(name, value)


def main(argv):
    """vpn2 creds publish | read <имя> | ensure <имя> (секрет - в stdout, только root)."""
    try:
        if argv in (['publish'], ['inbox']):
            (publish if argv == ['publish'] else take_inbox)()
            return 0
        if len(argv) == 2 and argv[0] in ('read', 'ensure') and argv[1] in NAMES:
            sys.stdout.write(read(argv[1]) if argv[0] == 'read' else ensure(argv[1]))
            return 0
    except (CredsError, OSError) as error:
        print(f'vpn2 creds: {error}', file=sys.stderr)
        return 1
    print(f'использование: vpn2 creds publish | inbox | read|ensure <{"|".join(NAMES)}>', file=sys.stderr)
    return 2
