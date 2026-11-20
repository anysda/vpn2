"""Ключ бэкапов: архив шифруется открытым ключом age (X25519), закрытый показывается один раз и хранится у человека.

Открытая часть - PUBLIC, не секрет: её читает задание бэкапа без хранилища. Метка UNSEEN рядом - пару ещё
никто не видел: деплой делает новую. Закрытой части на сервере нет; архивы 1.2.0 открывает их прежний пароль.
"""
import getpass
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

from vpn2 import fsutil
from vpn2.backup import env

PUBLIC = 'etc/anysda/backup.pub'
UNSEEN = 'etc/anysda/backup.pub.unseen'
BACKUP_ENV = 'etc/anysda/backup.env'
OLD_VARIABLE = 'ANYSDA_BACKUP_PASSPHRASE'
PREFIX = 'AGE-SECRET-KEY-1'
ROLLBACK = ('var/lib/vpn2-deploy/backups', 'var/lib/vpn2-deploy/panel/backups')
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


def adopt(public, root='/'):
    """Открытый ключ из архива: закрытый у человека, только что открывшего этот архив, - пара действует."""
    _at(PUBLIC, root).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fsutil.atomic(_at(PUBLIC, root), public.strip() + '\n', mode=0o644)
    seen(root)


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
    kept = _without_password(lines)
    if kept != lines:
        fsutil.atomic(path, ''.join(kept), mode=0o600)
    return value


def _without_password(lines):
    return [line for line in lines if not line.lstrip().removeprefix('export ').startswith(f'{OLD_VARIABLE}=')]


def scrub_rollback(root='/'):
    """Снимки отката деплоя берут /etc/anysda целиком: из их backup.env пароль архивов 1.2.0 уходит, сумма
    снимка в complete.json - новая, откат по снимку работает как прежде."""
    for top in ROLLBACK:
        for snapshot in sorted(_at(top, root).glob('*/configuration.tar')):
            with tarfile.open(snapshot) as tar:
                try:
                    lines = tar.extractfile(BACKUP_ENV).read().decode().splitlines(keepends=True)
                except (KeyError, AttributeError):
                    continue
                if (kept := _without_password(lines)) == lines:
                    continue
                members = [(m, tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()]
            body = ''.join(kept).encode()
            fresh = snapshot.with_name('configuration.tar.new')
            with tarfile.open(fresh, 'w') as out:
                for member, data in members:
                    if member.name == BACKUP_ENV:
                        member.size, data = len(body), body
                    out.addfile(member, None if data is None else io.BytesIO(data))
            os.chmod(fresh, 0o600)
            os.replace(fresh, snapshot)
            done = snapshot.with_name('complete.json')
            if done.is_file():
                sums = json.loads(done.read_text())
                sums['configuration.tar'] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
                fsutil.atomic(done, json.dumps(sums))


def shown_text(private, lost_archives=()):
    """Текст показа ключа: один раз, в конце установки; здесь же - что ключ не восстановить."""
    rows = ['', 'Ключ бэкапов - показан один раз, на сервере его нет:', f'  {private}',
            'Сохраните его вне сервера (менеджер паролей, бумага). ./deploy.sh restore спросит его.', LOST]
    if lost_archives:
        rows.append('Архивы, снятые с прежним непоказанным ключом, не расшифровать: ' + ', '.join(lost_archives))
    return '\n'.join(rows) + '\n'
