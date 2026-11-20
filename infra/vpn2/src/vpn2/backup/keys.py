"""Ключ бэкапов: архив шифруется открытым ключом age (X25519), закрытый показывается один раз и хранится у человека.

Открытая часть - PUBLIC, не секрет: её читает задание бэкапа без хранилища. Метка UNSEEN рядом - пару ещё
никто не видел: деплой делает новую. Закрытой части на сервере нет; архивы 1.2.0 открывает их прежний пароль.
"""
import getpass
import os
import subprocess
import sys
from pathlib import Path

from vpn2 import fsutil
from vpn2.backup import env

PUBLIC = 'etc/anysda/backup.pub'
UNSEEN = 'etc/anysda/backup.pub.unseen'
BACKUP_ENV = 'etc/anysda/backup.env'
OLD_VARIABLE = 'ANYSDA_BACKUP_PASSPHRASE'
PREFIX = 'AGE-SECRET-KEY-1'
LOST = 'Потеряете ключ - бэкапы не расшифровать: на сервере его нет, восстановить его нельзя.'
ASKED = {'key': ('архив зашифрован ключом бэкапов', 'Закрытый ключ бэкапов (AGE-SECRET-KEY-1…)'),
         'password': ('архив сделан до перехода на ключ (выпуск 1.2.0 и раньше) и зашифрован паролем',
                      'Пароль архивов, сделанных до перехода на ключ')}


class NoKey(Exception):
    """Ключ не введён или не того вида."""


def _at(rel, root='/'):
    return Path(root, rel)


def recipient(root='/'):
    """Открытый ключ бэкапов (age1…) или '' - пары нет."""
    try:
        return _at(PUBLIC, root).read_text().strip()
    except OSError:
        return ''


def unseen(root='/'):
    """Пару сделали, но закрытый ключ никому не показан: такая пара не годится."""
    return _at(UNSEEN, root).exists()


def issue(root='/'):
    """Новая пара: открытая часть - в PUBLIC с меткой «не показан», закрытая возвращается и нигде не остаётся.

    Вызывающий показывает ключ человеку (деплой - в конце установки, мастер - на экране бэкапа) и зовёт seen().
    """
    made = subprocess.run(['age-keygen'], capture_output=True, text=True, check=True).stdout
    private = next(line for line in made.splitlines() if line.startswith(PREFIX))
    public = subprocess.run(['age-keygen', '-y'], input=private + '\n', capture_output=True, text=True,
                            check=True).stdout.strip()
    _at(PUBLIC, root).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    _at(UNSEEN, root).touch(mode=0o644)
    fsutil.atomic(_at(PUBLIC, root), public + '\n', mode=0o644)
    return private


def seen(root='/'):
    """Закрытый ключ показан: пара действует, деплой её больше не меняет."""
    _at(UNSEEN, root).unlink(missing_ok=True)


def since_unseen(local_dir, root='/'):
    """Имена архивов в local_dir, снятых с непоказанной парой (не старше метки UNSEEN)."""
    try:
        mark = _at(UNSEEN, root).stat().st_mtime
        return sorted(e.name for e in os.scandir(local_dir) if e.name.endswith('.tar.gz.age')
                      and e.stat().st_mtime >= mark)
    except OSError:
        return []


def kind(path):
    """Чем зашифрован архив: 'key' (ключ бэкапов), 'password' (пароль 1.2.0) или '' - не архив age."""
    try:
        with open(path, 'rb') as handle:
            head = handle.read(4096).split(b'\n')
    except OSError:
        return ''
    if head[0] != b'age-encryption.org/v1':
        return ''
    stanzas = [line for line in head[1:] if line.startswith(b'-> ')]
    if any(line.startswith(b'-> scrypt ') for line in stanzas):
        return 'password'
    return 'key' if any(line.startswith(b'-> X25519 ') for line in stanzas) else ''


def ask(what, stdin=None, tell=None):
    """Ключ или пароль архива: с терминала без эха, без терминала - из stdin (файл или канал); в argv и
    окружение он не попадает. Что спрашивается, сказано на терминале или по tell. Не того вида - NoKey."""
    stdin = stdin or sys.stdin
    said, prompt = ASKED[what]
    if stdin.isatty() if tell is None else tell:
        print(f'anysda-restore: {said}', file=sys.stderr, flush=True)
    if stdin.isatty():
        lines = [getpass.getpass(f'{prompt}: ')]
    else:
        lines = stdin.read().splitlines()
    if what == 'key':
        value = next((line.strip() for line in lines if line.strip().startswith(PREFIX)), '')
        if not value:
            raise NoKey(f'{prompt}: введено не то - ключ начинается с {PREFIX}')
        return value
    value = next((line.strip() for line in lines if line.strip()), '')
    if not value:
        raise NoKey(f'{prompt}: ничего не введено')
    return value


def forget_password(root='/'):
    """Пароль архивов 1.2.0 из backup.env (ANYSDA_BACKUP_PASSPHRASE) убирается с диска; вернуть его ('' - не было)."""
    path = _at(BACKUP_ENV, root)
    try:
        lines = path.read_text().splitlines(keepends=True)
        value = env.read_env(path).get(OLD_VARIABLE, '')
    except (OSError, env.BackupEnvError):
        return ''
    kept = [line for line in lines if not line.lstrip().removeprefix('export ').startswith(f'{OLD_VARIABLE}=')]
    if kept != lines:
        fsutil.atomic(path, ''.join(kept), mode=0o600)
    return value


def shown_text(private, lost_archives=()):
    """Текст показа ключа: один раз, в конце установки; здесь же - что ключ не восстановить."""
    rows = ['', 'Ключ бэкапов - показан один раз, на сервере его нет:', f'  {private}',
            'Сохраните его вне сервера (менеджер паролей, бумага). ./deploy.sh restore спросит его.', LOST]
    if lost_archives:
        rows.append('Архивы, снятые с прежним непоказанным ключом, не расшифровать: ' + ', '.join(lost_archives))
    return '\n'.join(rows) + '\n'
