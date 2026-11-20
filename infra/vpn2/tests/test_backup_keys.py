"""Ключ бэкапов: пара age, показ один раз, пароль архивов 1.2.0 уходит с сервера, расшифровка по виду архива."""
import io
import json
import os
import shutil
import tarfile
from pathlib import Path

import pytest
from vpn2 import config, creds
from vpn2.backup import archive, keys
from vpn2.deploy import report
from vpn2.release import state

needs_age = pytest.mark.skipif(not (shutil.which('age') and shutil.which('age-keygen')), reason='нужен age')
EXAMPLE = Path(__file__).resolve().parents[3] / 'config.example.yaml'


@pytest.fixture
def entry(tmp_path, monkeypatch):
    """Корень entry во временном каталоге: /etc/anysda, каталог архивов и config.yaml."""
    root = tmp_path / 'root'
    (root / 'etc/anysda').mkdir(parents=True)
    backups = tmp_path / 'backups'
    backups.mkdir()
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(EXAMPLE.read_text().replace('    dir: /var/backups/anysda-vpn2', f'    dir: {backups}'))
    real = keys._at
    monkeypatch.setattr(keys, '_at', lambda rel, _root='/': real(rel, root))
    monkeypatch.setattr(report, 'CONFIG', cfg)
    monkeypatch.setattr(report, 'install_prereqs', lambda *_names: None)
    return root, backups, cfg


def rollback_snapshot(root, backup_env):
    """Снимок отката деплоя 1.2.0: /etc/anysda с backup.env, сумма в complete.json."""
    snapshot = root / 'var/lib/vpn2-deploy/panel/backups/1'
    snapshot.mkdir(parents=True)
    with tarfile.open(snapshot / 'configuration.tar', 'w') as tar:
        tar.addfile(_dir('etc/anysda'))
        for name, data in (('etc/anysda/backup.env', backup_env), ('etc/anysda/other', b'kept')):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    (snapshot / 'complete.json').write_text(json.dumps({'configuration.tar': state.sha256(snapshot / 'configuration.tar')}))
    return snapshot


def _dir(name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info


def disk_has(root, *needles):
    """Какая из строк лежит в файлах под root."""
    found = []
    for top, _dirs, files in os.walk(root):
        for name in files:
            data = Path(top, name).read_bytes()
            found += [needle for needle in needles if needle.encode() in data]
    return found


@needs_age
def test_pair_is_shown_once_and_never_regenerated(entry, capsys):
    root, _backups, _cfg = entry
    issued = report.backup_key_start(manager=False)
    private, public, lost = issued
    assert private.startswith('AGE-SECRET-KEY-1') and public.startswith('age1') and lost == []
    assert keys.recipient() == public and keys.unseen()
    assert not disk_has(root.parent, private), 'закрытый ключ на диске entry'
    report.show_backup_key(issued)
    out = capsys.readouterr().out
    assert private in out and keys.LOST in out and not keys.unseen()
    assert report.backup_key_start(manager=False) is False and keys.recipient() == public


@needs_age
def test_unseen_pair_of_an_interrupted_run_is_replaced(entry, capsys):
    root, backups, _cfg = entry
    report.backup_key_start(manager=False)
    first = keys.recipient()
    (backups / 'anysda-vpn2-2026-11-20T020000Z.tar.gz.age').write_text('x')
    private, public, lost = issued = report.backup_key_start(manager=False)
    assert public != first and lost == ['anysda-vpn2-2026-11-20T020000Z.tar.gz.age']
    report.show_backup_key(issued)
    assert 'прежним непоказанным ключом, не расшифровать: anysda-vpn2-2026-11-20T020000Z' in capsys.readouterr().out


@needs_age
@pytest.mark.usefixtures('entry')
def test_manager_run_makes_a_pair_but_shows_nothing():
    assert report.backup_key_start(manager=True) is False
    assert keys.recipient() and keys.unseen()
    public = keys.recipient()
    assert report.backup_key_start(manager=True) is False and keys.recipient() == public


def test_disabled_backups_make_no_pair(entry):
    _root, _backups, cfg = entry
    cfg.write_text(cfg.read_text().replace('backup:\n  enabled: true', 'backup:\n  enabled: false'))
    assert report.backup_key_start(manager=False) is False and not keys.recipient()


@needs_age
def test_upgrade_from_1_2_0_prints_key_and_old_password_together_then_drops_it(entry, capsys):
    """Обновление: пароль 1.2.0 - в хранилище (из config.yaml), в backup.env и в config.yaml; показ один раз, потом нигде."""
    root, _backups, cfg = entry
    cfg.write_text(cfg.read_text().replace('backup:\n  enabled: true', 'backup:\n  passphrase: "old 1.2.0 pass"\n  enabled: true'))
    creds.write('backup_passphrase', 'old 1.2.0 pass')
    (root / 'etc/anysda/backup.env').write_text("BACKUP_BACKEND=local\nANYSDA_BACKUP_PASSPHRASE='old 1.2.0 pass'\n")
    creds.write('backup_passphrase', '')
    snapshot = rollback_snapshot(root, (root / 'etc/anysda/backup.env').read_bytes())
    issued = report.backup_key_start(manager=False)
    state.verify_backup(snapshot)
    with tarfile.open(snapshot / 'configuration.tar') as tar:
        assert tar.extractfile('etc/anysda/backup.env').read() == b'BACKUP_BACKEND=local\n'
        assert tar.extractfile('etc/anysda/other').read() == b'kept'
    assert (root / 'etc/anysda/backup.env').read_text() == 'BACKUP_BACKEND=local\n', 'до снимков отката деплоя'
    assert creds.read('backup_passphrase') == 'old 1.2.0 pass'
    report.show_backup_key(issued)
    out = capsys.readouterr().out
    assert issued[0] in out and 'Пароль архивов, сделанных до перехода на ключ: old 1.2.0 pass' in out
    assert not creds.read('backup_passphrase') and not (creds.VIEWS / 'root/backup_passphrase').exists()
    assert (root / 'etc/anysda/backup.env').read_text() == 'BACKUP_BACKEND=local\n'
    assert 'old 1.2.0 pass' not in cfg.read_text() and config.loads(cfg.read_text())
    assert not disk_has(root.parent, issued[0], 'old 1.2.0 pass') and not disk_has(creds.STORE, 'old 1.2.0 pass')
    report.show_backup_key(report.backup_key_start(manager=False))
    assert capsys.readouterr().out == '', 'второй деплой ничего не показывает'


@needs_age
def test_pair_replaced_by_restore_is_not_shown(entry, capsys):
    issued = report.backup_key_start(manager=False)
    (entry[0] / keys.PUBLIC).write_text('age1fromthearchive\n')
    keys.seen()
    report.show_backup_key(issued)
    assert issued[0] not in capsys.readouterr().out


@needs_age
@pytest.mark.parametrize('damage', ['wrong key', 'other install', 'truncated'])
def test_decrypt_refuses_wrong_key_and_broken_archive(tmp_path, damage):
    private = keys.issue(tmp_path)
    plain = tmp_path / 'plain'
    plain.write_bytes(os.urandom(300_000))
    sealed = tmp_path / 'a.tar.gz.age'
    archive.encrypt(plain, sealed, keys.recipient(tmp_path))
    assert not plain.exists() and keys.kind(sealed) == 'key'
    archive.decrypt(sealed, tmp_path / 'out', private, 'key')
    (tmp_path / 'out').unlink()
    if damage == 'truncated':
        sealed.write_bytes(sealed.read_bytes()[:150_000])
    else:
        private = keys.issue(tmp_path / 'other') if damage == 'other install' else private[:-6] + 'QQQQQQ'
    with pytest.raises(archive.ArchiveError) as refused:
        archive.decrypt(sealed, tmp_path / 'out', private, 'key')
    assert refused.value.code == 4 and 'ничего не изменено' in refused.value.message
    assert not (tmp_path / 'out').exists()


def test_kind_is_read_from_the_archive(tmp_path):
    for body, what in ((b'age-encryption.org/v1\n-> scrypt c2FsdA 18\nx\n--- m\n', 'password'),
                       (b'age-encryption.org/v1\n-> X25519 c2hhcmU\nx\n--- m\n', 'key'), (b'\x1f\x8b plain gzip', '')):
        (tmp_path / 'a').write_bytes(body)
        assert keys.kind(tmp_path / 'a') == what


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_ask_reads_terminal_without_echo_or_stdin(monkeypatch, capsys):
    monkeypatch.setattr(keys.getpass, 'getpass', lambda prompt: 'AGE-SECRET-KEY-1TTY' if 'Закрытый ключ' in prompt else '')
    assert keys.ask('key', Tty()) == 'AGE-SECRET-KEY-1TTY'
    assert 'архив зашифрован ключом бэкапов' in capsys.readouterr().err
    saved = '# created: 2026-11-20\n# public key: age1x\nAGE-SECRET-KEY-1FILE\n'
    assert keys.ask('key', io.StringIO(saved)) == 'AGE-SECRET-KEY-1FILE'
    assert capsys.readouterr().err == '', 'без терминала и tell - молча'
    assert keys.ask('password', io.StringIO('old pass\n'), tell=True) == 'old pass'
    assert 'зашифрован паролем' in capsys.readouterr().err
    for what, typed in (('key', 'old pass\n'), ('key', ''), ('password', '\n')):
        with pytest.raises(keys.NoKey):
            keys.ask(what, io.StringIO(typed))
