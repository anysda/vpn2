"""Перезапуск службы только при смене её входов; формат общий с infra/lib/anysda-svc.sh."""

import hashlib
import subprocess
from pathlib import Path

from vpn2 import fsutil

APPLIED = '/var/lib/anysda/applied'


def fingerprint(paths):
    """sha256 склеенного содержимого файлов; чего нет, того нет и в отпечатке."""
    digest = hashlib.sha256()
    for path in paths:
        try:
            digest.update(Path(path).read_bytes())
        except OSError:
            continue
    return digest.hexdigest()


def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=False).returncode == 0


def forget(unit, applied=APPLIED):
    """Вход службы сменился так, что отпечаток файлов этого не заметит."""
    Path(applied, f'{unit}.sha256').unlink(missing_ok=True)


def restart_if_changed(unit, paths, applied=APPLIED, record=None):
    """paths - всё, что служба читает при старте; record - файл отпечатка вместо общего каталога.

    Вернуть, был ли перезапуск.
    """
    fp_file = Path(record or Path(applied, f'{unit}.sha256'))
    current = fingerprint(paths)
    if active(unit) and fp_file.is_file() and fp_file.read_text().strip() == current:
        print(f'{unit}: конфиг и бинарник прежние - не перезапускаю')
        return False
    subprocess.run(['systemctl', 'restart', unit], check=True)
    fp_file.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic(fp_file, current + '\n', mode=0o644)
    print(f'{unit} перезапущен')
    return True
