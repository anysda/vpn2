"""Съём бэкапа на поддельном узле под root и мелкие правила рядом: настройки, листинг, выбор архива."""
import fcntl
import json
import os
import shlex
import shutil
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from vpn2.backup import archive, env, listing, restore
from vpn2.backup import run as backup_run

needs_tools = pytest.mark.skipif(
    any(shutil.which(name) is None for name in ('bash', 'sqlite3', 'tar', 'shred')),
    reason='нужны bash, sqlite3, tar, shred')
SECRET = 'bot-token-value'
# age-заглушка: «шифрование» - копия файла, пароль с терминала не читается.
STUBS = {
    'age': '#!/bin/sh\n[ -z "$STUB_AGE_FAIL" ] || exit "$STUB_AGE_FAIL"\ncp "$4" "$3"\n',
    'git': '#!/bin/sh\necho abc1234\n',
    'curl': '#!/bin/sh\necho \'{"version":"9.9"}\'\n',
    'rclone': '#!/bin/sh\necho "rclone $*" >> "$STUB_LOG"\n[ "$1" != lsl ] || cat "$STUB_S3_LIST"\n'
              '[ "$STUB_RCLONE_FAIL" != "$1" ] || { echo "rclone: boom" >&2; exit 9; }\n',
}


def put(path, data='', mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    if mode:
        path.chmod(mode)
    return path


def make_node(root):
    """Поддельный узел в root: те же пути, что на настоящем, внутри root."""
    paths = SimpleNamespace(
        root=str(root), env_file=f'{root}/etc/anysda/backup.env', etc_anysda=f'{root}/etc/anysda',
        wireguard=f'{root}/etc/wireguard', db=f'{root}/var/lib/anysda-vpn2/db.sqlite',
        source_root_file=f'{root}/etc/anysda/source-root', work_parent=f'{root}/var/lib/anysda-vpn2')
    runtime = root / 'var/lib/anysda-vpn2/runtime'
    etc = root / 'etc'
    for name in ('sso-runtime.json', 'telegram-runtime.json'):
        put(runtime / name, '{"x": 1}\n')
        (etc / 'anysda').mkdir(parents=True, exist_ok=True)
        (etc / 'anysda' / name).symlink_to(runtime / name)
    put(etc / 'anysda/admin-password', 'pw\n', 0o600)
    put(etc / 'anysda/tgbot-secret.txt', SECRET + '\n', 0o600)
    put(etc / 'anysda/файл с пробелом.txt', 'данные\n')
    put(etc / 'anysda/source-root', '/nonexistent/src\n')
    for name in ('wg0.conf', 'server.priv', 'server.pub'):
        put(etc / 'wireguard' / name, name + '\n', 0o600)
    put(etc / 'openvpn/server/.pki.lock')
    put(etc / 'openvpn/ca.crt', 'ca\n')
    put(etc / 'openvpn/ccd/client one', 'ifconfig-push\n')
    os.mkfifo(etc / 'openvpn/mgmt.fifo')
    put(etc / 'strongswan/ca.pem', 'ike\n')
    put(etc / 'swanctl/swanctl.conf', 'conns\n')
    for name in ('strongswan.conf', 'ipsec.conf', 'ipsec.secrets', 'strongswan.d/x.conf'):
        put(etc / name, name + '\n')
    put(root / 'var/lib/caddy/.local/share/caddy/certificates/сертификат b.crt', 'crt\n')
    put(root / 'var/lib/caddy/.local/share/caddy/locks/l.lock', 'x')
    put(root / 'var/lib/anysda-vpn2/state/mgmt-ips.txt', 'exit-a 10.0.0.2\n', 0o600)
    conn = sqlite3.connect(paths.db)
    conn.execute('create table t (a)')
    conn.execute("insert into t values ('x')")
    conn.commit()
    conn.close()
    (root / 'out dir').mkdir()
    return paths


def values(root, **extra):
    return {'BACKUP_BACKEND': 'local', 'BACKUP_LOCAL_DIR': str(root / 'out dir'), 'BACKUP_RETENTION': '10',
            'ANYSDA_BACKUP_PASSPHRASE': 'парольная фраза', **extra}


def write_env(root, vals):
    put(root / 'etc/anysda/backup.env', '\n'.join(f'{k}={shlex.quote(v)}' for k, v in vals.items()) + '\n', 0o600)


def create(root, paths, vals=None, **kw):
    vals = vals or values(root)
    write_env(root, vals)
    return backup_run.create(env.settings(vals, paths.env_file),
                             paths=backup_run.Paths(env_file=paths.env_file, root=paths.root),
                             **{'check': lambda _bundle: (0, ''), 'pause': 0, **kw})


@pytest.fixture
def node(tmp_path, monkeypatch):
    root = tmp_path / 'n'
    root.mkdir()
    paths = make_node(root)
    for name, body in STUBS.items():
        put(root / 'bin' / name, body, 0o755)
    monkeypatch.setenv('PATH', f'{root}/bin:/usr/bin:/bin')
    monkeypatch.setenv('STUB_LOG', str(root / 'log'))
    events = []
    monkeypatch.setattr(backup_run.notify, 'panel_event', lambda event, **kw: events.append((event, kw)))
    return root, paths, events


@needs_tools
def test_create_archive_manifest_and_summary(node, capsys):
    root, paths, events = node
    assert create(root, paths) == 0
    (final,) = (root / 'out dir').iterdir()
    assert archive.NAME.fullmatch(final.name) and oct(final.stat().st_mode & 0o777) == '0o600'
    out = capsys.readouterr().out
    assert f"\n{'backup:':<12} {final}\n{'size:':<12} {final.stat().st_size} bytes\n" in out
    assert out.endswith(f"{'backend:':<12} local\n{'retention:':<12} keep last 10\n")
    with tarfile.open(final) as tar:
        tar.extractall(root / 'x', filter='tar')
    bundle = root / 'x' / final.name.removesuffix('.tar.gz.age')
    manifest = json.loads((bundle / 'manifest.json').read_text())
    assert list(manifest) == ['version', 'git_commit', 'schema_version', 'timestamp', 'components',
                              'archive_format', 'spec_version']
    assert manifest['components'] == archive.components(bundle)
    assert {'db.sqlite', 'etc-anysda', 'etc-wireguard', 'etc-openvpn', 'caddy-storage',
            'deploy-state'} <= set(manifest['components'])
    assert (bundle / 'etc-anysda/sso-runtime.json').read_text() == '{"x": 1}\n'
    assert not (bundle / 'etc-anysda/sso-runtime.json').is_symlink()
    assert not (bundle / 'etc-openvpn/mgmt.fifo').exists()
    assert restore.check_manifest(str(bundle), final.name) == manifest
    assert not events and not list(Path(paths.work_parent).glob('.backup-tmp.*'))


@needs_tools
def test_create_busy_lock_reports_and_cleans(node):
    root, paths, events = node
    fd = os.open(f'{root}/etc/openvpn/server/.pki.lock', os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert create(root, paths, lock_wait=0.2) == 1
    finally:
        os.close(fd)
    assert events[0][0]['type'] == 'backup_failed'
    assert 'замок PKI OpenVPN занят дольше минуты' in events[0][0]['detail']
    assert not list(Path(paths.work_parent).glob('.backup-tmp.*'))


@needs_tools
def test_mismatch_in_every_slice_is_warning_code_4(node, capsys):
    root, paths, events = node
    assert create(root, paths, check=lambda _bundle: (3, 'устройство 7: нет пира WireGuard')) == 4
    captured = capsys.readouterr()
    assert captured.err.count('снимаю заново:') == 9
    assert f"{'warning:':<12} БД и конфиги VPN расходятся (устройства: 7) — перевыпусти в панели" in captured.out
    assert events[0][0]['type'] == 'backup_warning' and len(list((root / 'out dir').iterdir())) == 1


@needs_tools
def test_empty_passphrase_is_one_event(node, capsys):
    root, paths, events = node
    assert create(root, paths, values(root, ANYSDA_BACKUP_PASSPHRASE='')) == 2
    err = capsys.readouterr().err
    assert err.startswith('anysda-backup FATAL: ANYSDA_BACKUP_PASSPHRASE отсутствует или пуста.')
    assert paths.env_file in err
    assert [event for event, _kw in events] == [{'type': 'backup_failed', 'detail': 'нет пароля шифрования'}]
    assert events[0][1]['secret_file'] == f'{root}/etc/anysda/tgbot-secret.txt'


@needs_tools
def test_s3_upload_and_rotation(node, monkeypatch, capsys):
    root, paths, _events = node
    old = [f'anysda-vpn2-2020-01-0{n}T000000Z.tar.gz.age' for n in (1, 2, 3)]
    put(root / 's3', ''.join(f'  10 2020-01-0{n} 00:00:00.000000000 {name}\n' for n, name in enumerate(old, 1)))
    monkeypatch.setenv('STUB_S3_LIST', str(root / 's3'))
    s3 = {'BACKUP_BACKEND': 's3', 'BACKUP_RETENTION': '2', 'BACKUP_S3_BUCKET': 'b', 'BACKUP_S3_ENDPOINT': 'https://e',
          'ANYSDA_S3_ACCESS_KEY': 'k', 'ANYSDA_S3_SECRET_KEY': 's'}
    assert create(root, paths, values(root, **s3)) == 0
    log = (root / 'log').read_text().splitlines()
    assert log[0].startswith('rclone copyto ') and ' s3:b/anysda-vpn2-' in log[0]
    assert f'rclone deletefile s3:b/{old[0]}' in log
    assert f's3 rotated out {old[0]}' in capsys.readouterr().out
    monkeypatch.setenv('STUB_RCLONE_FAIL', 'copyto')
    assert create(root, paths, values(root, **s3)) == 9


S3 = {'BACKUP_BACKEND': 's3', 'BACKUP_S3_BUCKET': 'b', 'BACKUP_S3_ENDPOINT': 'https://e',
      'ANYSDA_S3_ACCESS_KEY': 'access-key', 'ANYSDA_S3_SECRET_KEY': 'secret-key'}


@needs_tools
@pytest.mark.parametrize(('extra', 'stub_env', 'code', 'detail'), [
    ({}, {'STUB_AGE_FAIL': '7'}, 7, 'упаковка и шифрование не прошли: age: код выхода 7'),
    (S3, {'STUB_RCLONE_FAIL': 'copyto'}, 9, 'выгрузка в S3 не прошла: rclone: boom'),
    (S3, {}, 1, 'выгрузка в S3 не прошла: архива нет в списке бакета'),
    ({}, {'STUB_SQLITE': 'database or disk is full'}, 1,
     'снимок базы и ключей не снят: нет места на диске'),
    (S3, {'STUB_SQLITE': 'bad access-key secret-key парольная фраза'}, 1,
     'снимок базы и ключей не снят: sqlite3: bad *** *** ***'),
])
def test_failure_event_names_step_and_cause(node, monkeypatch, extra, stub_env, code, detail):
    """Событие о сбое (панель шлёт его админу в Telegram) называет шаг и причину, без секретов."""
    root, paths, events = node
    if 'STUB_SQLITE' in stub_env:
        def broken(*_args):
            raise sqlite3.OperationalError(stub_env.pop('STUB_SQLITE'))
        monkeypatch.setattr(backup_run, 'db_copy', broken)
    put(root / 's3', '')
    monkeypatch.setenv('STUB_S3_LIST', str(root / 's3'))
    for name, value in stub_env.items():
        monkeypatch.setenv(name, value)
    assert create(root, paths, values(root, **extra)) == code
    assert [event for event, _kw in events] == [{'type': 'backup_failed', 'detail': detail}]
    assert not list((root / 'var/lib/anysda-vpn2').glob('.backup-tmp.*'))


@needs_tools
def test_full_disk_at_the_archive_directory_is_named(node, monkeypatch):
    root, paths, events = node

    def move(_source, target):
        raise OSError(28, 'No space left on device', target)
    monkeypatch.setattr(backup_run.shutil, 'move', move)
    assert create(root, paths) == 1
    assert events[0][0]['detail'].startswith(f"архив не сохранён на диск: нет места на диске ({root}/out dir/anysda-")


def test_rotate_local_keeps_newest(tmp_path, capsys):
    for index in range(4):
        item = put(tmp_path / f'anysda-vpn2-{index}.tar.gz.age', 'x')
        os.utime(item, (100 + index, 100 + index))
    put(tmp_path / 'other.txt', 'x')
    backup_run.rotate_local(str(tmp_path), str(tmp_path / 'anysda-vpn2-3.tar.gz.age'), 2)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        'anysda-vpn2-2.tar.gz.age', 'anysda-vpn2-3.tar.gz.age', 'other.txt']
    assert capsys.readouterr().out.count('rotated out') == 2


def test_settings_defaults_and_required():
    got = env.settings({'BACKUP_BACKEND': 'local'})
    assert (got.local_dir, got.retention, got.s3_region) == ('/var/backups/anysda-vpn2', '10', 'us-east-1')
    with pytest.raises(env.BackupEnvError, match='BACKUP_BACKEND не задан в /x') as caught:
        env.settings({}, '/x')
    assert caught.value.code == 1
    with pytest.raises(env.BackupEnvError, match='BACKUP_S3_BUCKET не задан'):
        env.settings({'BACKUP_BACKEND': 's3'})
    lazy = env.settings({'BACKUP_BACKEND': 's3', 'ANYSDA_BACKUP_PASSPHRASE': 'секрет'}, lazy_s3=True)
    assert 'секрет' not in repr(lazy)
    assert env.s3(lazy, 'lsl', 's3:') == (1, '', 'BACKUP_S3_BUCKET: parameter null or not set')


def test_read_env_is_what_bash_sources(tmp_path):
    path = put(tmp_path / 'backup.env', "A='один два'\nB=$'x\\ny'\nexport C=\"$A!\"\n")
    assert env.read_env(path) == {'A': 'один два', 'B': 'x\ny', 'C': 'один два!'}
    with pytest.raises(env.BackupEnvError) as caught:
        env.read_env(tmp_path / 'нет')
    assert caught.value.code == 2


@pytest.mark.parametrize(('n', 'text'), [(0, '0'), (1023, '1023'), (1024, '1.0K'), (1025, '1.1K'),
                                         (10 * 1024, '10K'), (10 * 1024 + 1, '11K'), (1024 ** 2, '1.0M')])
def test_human_size_like_numfmt(n, text):
    assert listing.human_sizes([n]) == [text]


def test_listing_local_rows(tmp_path, capsys):
    for index, name in enumerate(('anysda-vpn2-2026-01-01T000000Z.tar.gz.age', 'anysda-vpn2-x.tar.gz.age')):
        os.utime(put(tmp_path / name, 'x' * 2048), (100 + index, 100 + index))
    put(tmp_path / 'other', 'x')
    assert listing.render(env.settings({'BACKUP_BACKEND': 'local', 'BACKUP_LOCAL_DIR': str(tmp_path)})) == 0
    row = '%-50s %-22s %12s'
    assert capsys.readouterr().out.splitlines()[2:] == [
        row % ('anysda-vpn2-x.tar.gz.age', '?', '2.0K'),
        row % ('anysda-vpn2-2026-01-01T000000Z.tar.gz.age', '2026-01-01T000000Z', '2.0K')]


def test_parse_args_and_age_text():
    assert restore.parse_args([]) == restore.Args()
    assert restore.parse_args(['x.age', '-f', '--allow-incompatible']) == restore.Args(
        name='x.age', force=True, allow_incompatible=True)
    assert restore.parse_args(['--dry-run']).name == 'latest'
    with pytest.raises(restore.RestoreError):
        restore.parse_args(['latest', 'second'])
    assert [restore.age_text(s) for s in (59, 3600, 90061, -90)] == ['0 мин', '1 ч 0 мин', '1 д 1 ч', '-1 мин']


def test_latest_prefers_strictly_newer_cloud(tmp_path, monkeypatch, capsys):
    old, new = (f'anysda-vpn2-2026-01-0{day}T000000Z.tar.gz.age' for day in (1, 2))
    put(tmp_path / old)
    put(tmp_path / 'anysda-vpn2-junk.tar.gz.age')
    vals = env.settings({'BACKUP_BACKEND': 's3', 'BACKUP_LOCAL_DIR': str(tmp_path)}, lazy_s3=True)
    listed = {'code': 0, 'text': f'10 2026-01-02 00:00:00.0 {new}\n10 2026-01-01 00:00:00.0 {old}'}
    monkeypatch.setattr(env, 's3', lambda *_a, **_k: (listed['code'], listed['text'], ''))
    assert restore.resolve_latest(vals) == (new, 'S3')
    assert f'самый свежий local: {old} — старше выбранного на 1 д 0 ч' in capsys.readouterr().err
    listed.update(code=1, text='boom')
    assert restore.resolve_latest(vals) == (old, 'local')
    assert 'WARN: S3 недоступен' in capsys.readouterr().err
    (tmp_path / old).unlink()
    with pytest.raises(restore.RestoreError) as caught:
        restore.resolve_latest(vals)
    assert caught.value.code == 3
