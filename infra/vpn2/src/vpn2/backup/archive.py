"""Архив бэкапа: рабочий каталог, tar.gz, шифрование age через pty (вместо expect в bash)."""
import contextlib
import fcntl
import os
import pty
import select
import signal
import shutil
import subprocess
import tempfile
import termios
import time
from pathlib import Path

ENTER_PROMPT = b'Enter passphrase'
CONFIRM_PROMPT = b'Confirm passphrase'
TIMEOUT = 60
_READ = 4096


class ArchiveError(Exception):
    """Шаг архива не удался; code - код выхода скрипта (3 шифрование, 4 расшифровка, 5 распаковка)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def wipe(path, *, run=subprocess.run):
    """`shred -uz path`, а если shred нет или он отказал - просто удалить (как `|| rm -f`)."""
    try:
        done = run(['shred', '-uz', str(path)], capture_output=True, check=False)
        failed = done.returncode != 0
    except OSError:
        failed = True
    if failed:
        with contextlib.suppress(OSError):
            os.unlink(path)


@contextlib.contextmanager
def workdir(parent, prefix, *, run=subprocess.run):
    """Каталог 0700, как `mktemp -d parent/prefixXXXXXX`; на выходе (и при исключении) уборка.

    Уборка как в скрипте: shred -uz каждого обычного файла (отказ shred не мешает), затем rm -rf.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    os.chmod(path, 0o700)
    try:
        yield path
    finally:
        if path.is_dir():
            for root, _dirs, files in os.walk(path):
                for name in files:
                    item = os.path.join(root, name)
                    if os.path.isfile(item) and not os.path.islink(item):
                        with contextlib.suppress(OSError):
                            run(['shred', '-uz', item], capture_output=True, check=False)
            shutil.rmtree(path, ignore_errors=True)


def _terminated(signum, _frame):
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def exit_on_signals(signals=(signal.SIGTERM, signal.SIGHUP)):
    """TERM и HUP внутри блока - SystemExit(128+N): уборка рабочих каталогов идёт, как по trap EXIT."""
    saved = {s: signal.signal(s, _terminated) for s in signals}
    try:
        yield
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)


def pack(work_dir, bundle_name, out_tar, *, run=subprocess.run):
    """`tar -C work_dir -czf out_tar bundle_name`: в архив кладётся сам каталог набора."""
    args = ['tar', '-C', str(work_dir), '-czf', str(out_tar), bundle_name]
    try:
        done = run(args, capture_output=True, check=False)
    except OSError as exc:
        raise ArchiveError(127, f'anysda-backup: tar не запущен: {exc.strerror}') from None
    if done.returncode != 0:
        raise ArchiveError(done.returncode, 'anysda-backup: tar -czf провалился')


def pty_run(argv, passphrase, prompts, *, timeout=TIMEOUT):
    """Запустить argv на терминале и отвечать паролем на приглашения; вернуть код выхода.

    Как expect: каждое приглашение из prompts (подстрока вывода) - пароль и перевод строки
    (\\r), после каждого ответа таймаут отсчитывается заново. Дочерний процесс - лидер сессии
    с управляющим терминалом; пароль не попадает ни в argv, ни в окружение. Тишина дольше
    timeout - процесс убит, код отрицательный.
    """
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                                start_new_session=True,
                                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    except OSError:
        os.close(master)
        os.close(slave)
        return 127
    os.close(slave)
    answer = passphrase.encode() + b'\r'
    buffer = b''
    deadline = time.monotonic() + timeout
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([master], [], [], left)[0]:
                proc.kill()
                break
            try:
                chunk = os.read(master, _READ)
            except OSError:
                break
            if not chunk:
                break
            buffer = (buffer + chunk)[-_READ:]
            if any(p in buffer for p in prompts):
                os.write(master, answer)
                buffer = b''
                deadline = time.monotonic() + timeout
    finally:
        os.close(master)
    return proc.wait()


def _age(args, passphrase, prompts, run, timeout):
    try:
        return run(args, passphrase, prompts, timeout=timeout)
    except OSError:
        return 127


def encrypt(src, dst, passphrase, *, run=pty_run, timeout=TIMEOUT, age='age', wiper=wipe):
    """`age -p -o dst src` с паролем через pty (приглашения Enter и Confirm); src затирается.

    Не удалось или файла нет/пуст - ArchiveError(3). Текст ошибки пароль не содержит.
    """
    prompts = (ENTER_PROMPT, CONFIRM_PROMPT)
    _age([age, '-p', '-o', str(dst), str(src)], passphrase, prompts, run, timeout)
    wiper(src)
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise ArchiveError(3, f'anysda-backup FATAL: шифрование age не дало файла {dst}')


def decrypt(src, dst, passphrase, *, run=pty_run, timeout=TIMEOUT, age='age'):
    """`age -d -o dst src` с паролем через pty (приглашение Enter); при отказе dst удаляется.

    Код age не 0 или dst пуст - ArchiveError(4).
    """
    code = _age([age, '-d', '-o', str(dst), str(src)], passphrase, (ENTER_PROMPT,), run, timeout)
    if code != 0 or not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        with contextlib.suppress(OSError):
            os.unlink(dst)
        raise ArchiveError(4, f'anysda-restore: дешифровка провалилась (age exit={code})'
                              ' — неверный passphrase или повреждённый архив')


def unpack(tar_path, dest, *, run=subprocess.run):
    """`tar -C dest -xzf tar_path`; отказ - ArchiveError(5), stderr tar в тексте с отступом в 2 пробела."""
    try:
        done = run(['tar', '-C', str(dest), '-xzf', str(tar_path)], capture_output=True, check=False)
    except OSError as exc:
        raise ArchiveError(5, f'anysda-restore: tar не запущен: {exc.strerror}') from None
    if done.returncode != 0:
        lines = done.stderr.decode(errors='replace').splitlines()
        text = 'anysda-restore: tar -xzf провалился — архив повреждён (после успешной дешифровки)'
        raise ArchiveError(5, '\n'.join([text, *('  ' + line for line in lines)]))
