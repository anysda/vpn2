"""Архив бэкапа: рабочий каталог, sha компонентов, tar.gz, шифрование age открытым ключом и расшифровка."""
import calendar
import contextlib
import fcntl
import hashlib
import os
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
from pathlib import Path

from vpn2 import fsutil

MANIFEST = 'manifest.json'
NAME = re.compile(r'anysda-vpn2-([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z)\.tar\.gz\.age')
TIMEOUT = 60


class ArchiveError(Exception):
    """Шаг не удался; code - код выхода команды."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def stamp_epoch(stamp):
    """Метка вида 2026-05-22T020000Z в секунды эпохи (UTC)."""
    return calendar.timegm(time.strptime(stamp, '%Y-%m-%dT%H%M%SZ'))


def is_archive(entry):
    """Запись os.scandir - файл архива anysda-vpn2-*.tar.gz.age."""
    return (entry.name.startswith('anysda-vpn2-') and entry.name.endswith('.tar.gz.age')
            and len(entry.name) >= len('anysda-vpn2-.tar.gz.age') and entry.is_file(follow_symlinks=False))


def code(argv, **kwargs):
    """Код программы; не запустилась - 127, как у shell."""
    try:
        return subprocess.run(argv, check=False, **kwargs).returncode
    except OSError:
        return 127


def call(argv, **kwargs):
    """Программа, чей stderr виден оператору: (код, stdout, последняя непустая строка stderr)."""
    try:
        done = subprocess.run(argv, check=False, stderr=subprocess.PIPE, text=True, errors='surrogateescape', **kwargs)
    except OSError as exc:
        print(f'{argv[0]}: {exc.strerror}', file=sys.stderr)
        return 127, '', f'программа {argv[0]} не найдена'
    sys.stderr.write(done.stderr)
    lines = [line.strip() for line in done.stderr.splitlines() if line.strip()]
    return done.returncode, (done.stdout or '').rstrip('\n'), lines[-1] if lines else ''


def sha_file(path):
    with open(path, 'rb') as source:
        return 'sha256:' + hashlib.file_digest(source, 'sha256').hexdigest()


def sha(path):
    """sha256 компонента набора: файла или каталога (отсортированный обход: путь, 0, sha файла, перевод строки)."""
    if not os.path.isdir(path):
        return sha_file(path)
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            try:
                item = sha_file(full).encode()
            except OSError:
                item = b'<unreadable>'
            digest.update(os.path.relpath(full, path).encode() + b'\0' + item + b'\n')
    return 'sha256:' + digest.hexdigest()


def components(bundle_dir):
    """Имя -> sha для всего в каталоге набора, кроме manifest.json, по порядку имён."""
    return {name: sha(os.path.join(bundle_dir, name)) for name in sorted(os.listdir(bundle_dir)) if name != MANIFEST}


def wipe(path):
    """`shred -uz path`, а если shred нет или он отказал - просто удалить."""
    if code(['shred', '-uz', str(path)], capture_output=True):
        with contextlib.suppress(OSError):
            os.unlink(path)


@contextlib.contextmanager
def workdir(parent, prefix):
    """Каталог 0700 в parent; на выходе обычные файлы затираются shred, каталог удаляется."""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield path
    finally:
        for root, _dirs, files in os.walk(path):
            for name in files:
                item = os.path.join(root, name)
                if os.path.isfile(item) and not os.path.islink(item):
                    code(['shred', '-uz', item], capture_output=True)
        shutil.rmtree(path, ignore_errors=True)


def _terminated(signum, _frame):
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def exit_on_signals(signals=(signal.SIGTERM, signal.SIGHUP)):
    """TERM и HUP внутри блока - SystemExit(128+N): рабочие каталоги успевают убраться."""
    saved = {s: signal.signal(s, _terminated) for s in signals}
    try:
        yield
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)


def pack(directory, name, out_tar):
    with tarfile.open(out_tar, 'w:gz') as tar:
        tar.add(os.path.join(directory, name), arcname=name)


def drop_special_files(tree):
    """Сокеты, каналы и устройства из скопированных живых каталогов: их не хешируют и не архивируют."""
    for top, _, files in os.walk(tree):
        for name in files:
            path = os.path.join(top, name)
            if stat.S_IFMT(os.lstat(path).st_mode) in (stat.S_IFSOCK, stat.S_IFIFO, stat.S_IFBLK, stat.S_IFCHR):
                os.unlink(path)


def unpack(tar_path, dest):
    try:
        with tarfile.open(tar_path) as tar:
            tar.extractall(dest, filter='tar')
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveError(5, 'anysda-restore: tar -xzf провалился — архив повреждён (после успешной дешифровки)\n'
                              f'  {exc}') from None


def pty_run(argv, passphrase, prompts, *, timeout=TIMEOUT):
    """argv на своём терминале, на приглашения отвечает паролем; код выхода.

    Пароль не попадает ни в argv, ни в окружение. Тишина дольше timeout после
    последнего ответа - процесс убит, код отрицательный.
    """
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True,
                                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    except OSError:
        os.close(master)
        return 127
    finally:
        os.close(slave)
    buffer = b''
    deadline = time.monotonic() + timeout
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([master], [], [], left)[0]:
                proc.kill()
                break
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            buffer = (buffer + chunk)[-4096:]
            if any(prompt in buffer for prompt in prompts):
                os.write(master, passphrase.encode() + b'\r')
                buffer = b''
                deadline = time.monotonic() + timeout
    finally:
        os.close(master)
    return proc.wait()


def encrypt(src, dst, recipient):
    """`age -r <открытый ключ> -o dst src`; src затирается. Отказ - ArchiveError (код age или 3)."""
    rc = code(['age', '-r', recipient, '-o', str(dst), str(src)])
    wipe(src)
    if rc or not fsutil.filled(dst):
        raise ArchiveError(rc if rc > 0 else 3, f'anysda-backup FATAL: шифрование age не дало файла {dst}')


def _with_identity(argv, private):
    """age с закрытым ключом из канала /dev/fd/N: ключ не ложится на диск и не виден в argv."""
    read, write = os.pipe()
    os.write(write, private.encode() + b'\n')
    os.close(write)
    try:
        return code([*argv[:2], '-i', f'/dev/fd/{read}', *argv[2:]], pass_fds=(read,), stderr=subprocess.DEVNULL)
    finally:
        os.close(read)


def decrypt(src, dst, secret, what):
    """Расшифровать src в dst: what 'key' - закрытым ключом бэкапов, 'password' - паролем архива 1.2.0 через pty.
    Отказ - dst удаляется и ArchiveError(4)."""
    argv = ['age', '-d', '-o', str(dst), str(src)]
    rc = _with_identity(argv, secret) if what == 'key' else pty_run(argv, secret, (b'Enter passphrase',))
    if rc or not fsutil.filled(dst):
        with contextlib.suppress(OSError):
            os.unlink(dst)
        wrong = 'неверный ключ бэкапов' if what == 'key' else 'неверный пароль'
        raise ArchiveError(4, f'anysda-restore: архив не расшифрован (age exit={rc}) — {wrong} или архив повреждён;'
                              ' ничего не изменено')
