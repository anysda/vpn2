import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from vpn2.backup import prepare
from vpn2.backup.archive import ArchiveError
from vpn2.backup.errors import RestoreError

SCRIPT = Path(__file__).resolve().parent / 'fixtures' / 'backup' / 'anysda-restore.sh'
HAS_BASH = shutil.which('bash') is not None and SCRIPT.is_file()
BASH = pytest.mark.skipif(not HAS_BASH, reason='нет bash или anysda-restore.sh')
SQLITE = pytest.mark.skipif(not HAS_BASH or shutil.which('sqlite3') is None, reason='нет sqlite3')


def block(start, stop):
    """Строки скрипта от первой, начинающейся с start, до строки перед stop."""
    lines = SCRIPT.read_text().split('\n')
    first = next(i for i, line in enumerate(lines) if line.startswith(start))
    last = next(i for i, line in enumerate(lines) if i > first and line.startswith(stop))
    return '\n'.join(lines[first:last])


def bash(code, env=None, path=None):
    """Блок из скрипта в bash с set -euo pipefail (как у скрипта)."""
    full = dict(os.environ, **(env or {}))
    if path:
        full['PATH'] = f'{path}:{full["PATH"]}'
    done = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + code], capture_output=True, env=full, check=False)
    return done.returncode, done.stderr.decode()


def stub(directory, name, body):
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text('#!/bin/bash\n' + body)
    path.chmod(0o755)


# --- check_bundle_report -------------------------------------------------

CHECK_CASES = [
    (0, 'всё ок'),
    (3, 'CRL расходится\nпиры WireGuard не те'),
    (3, 'IKEv2 учётка'),
    (3, 'что-то иное'),
    (5, 'нет права на netns'),
    (1, 'упало'),
    (7, ''),
]


def collect():
    lines = []
    return lines, lines.append


@pytest.mark.parametrize(('code', 'text'), CHECK_CASES)
def test_check_report_matches_bash_block(tmp_path, code, text):
    """Прямая сверка с блоком 305-322 (удалить вместе с bash-телом). Замена: путь CHECK -> заглушка."""
    stub(tmp_path, 'check.sh', f'printf "%s" {shlex.quote(text)}; exit {code}\n')
    checker = tmp_path / 'check.sh'
    body = block('CHECK=/usr/local', '# Старые архивы содержали').replace('/usr/local/bin/anysda-backup.sh', str(checker))
    _, expected = bash(f'BUNDLE_DIR=/b\n{body}')
    lines, out = collect()
    assert prepare.check_bundle_report('/b', checker=str(checker), out=out) == code
    assert ''.join(f'{line}\n' for line in lines) == expected


def test_check_report_values_and_skips(tmp_path):
    lines, out = collect()
    assert prepare.check_bundle_report('/b', checker=str(tmp_path / 'нет'), out=out) == 0
    assert lines == []
    prepare.check_bundle_report('/b', checker=lambda _b: (3, 'IKEv2 и CRL'), out=out)
    assert lines == ['anysda-restore: WARN: в архиве БД и CRL OpenVPN, учётки IKEv2 расходятся'
                     ' — после восстановления перевыпусти эти устройства в панели:', 'IKEv2 и CRL']
    lines.clear()
    prepare.check_bundle_report('/b', checker=lambda _b: (3, 'x'), out=out)
    assert 'и конфиги VPN расходятся' in lines[0]
    lines.clear()
    prepare.check_bundle_report('/b', checker=lambda _b: (9, 'y'), out=out)
    assert lines == ['anysda-restore: WARN: сверка БД с конфигами VPN не выполнена (код 9):', 'y']


def test_check_report_calls_checker_like_script(tmp_path):
    checker = tmp_path / 'c.sh'
    checker.write_text('#!/bin/sh\n')
    checker.chmod(0o755)
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(returncode=0, stdout=b'')

    prepare.check_bundle_report('/набор 1', checker=str(checker), run=run, out=lambda _t: None)
    assert calls[0][0] == [str(checker), '--check-bundle', '/набор 1']
    assert calls[0][1]['stderr'] == subprocess.STDOUT


# --- wg_key_material -------------------------------------------------------

FAKE_PUB = 'PUBFIXED'
WG_STUB = f'cat >> "$WGLOG"; echo {FAKE_PUB}\n'
CONFS = {
    'plain': b'[Interface]\nAddress = 10.0.0.1/24\nPrivateKey = AAAA=\nListenPort = 1\n',
    'quirky': b'# c\n[ interface ]x\n[interface]\n pRiVaTeKeY \t= B B=# comm\r\n[Peer]\nPrivateKey=PEER\n',
    'last_wins': b'[Interface]\nPrivateKey=ONE\nPrivateKey=TWO\n',
    'nul': b'[Interface]\nPriv\0ateKey=NUL1\n',
    'peer_only': b'[Peer]\nPrivateKey=PEER\n',
    'empty': b'',
}


def make_wg(root, conf, extra=()):
    wg = root / 'etc-wireguard'
    wg.mkdir(parents=True)
    if conf is not None:
        (wg / 'wg0.conf').write_bytes(conf)
    for name, data in extra:
        (wg / name).write_bytes(data)
    return wg


def snapshot_dir(wg):
    return {p.name: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in sorted(wg.iterdir())
            if p.is_file() and not p.is_symlink()}


@BASH
@pytest.mark.parametrize('name', CONFS)
def test_wg_key_matches_bash_block(tmp_path, name):
    """Прямая сверка с блоком 327-355 (удалить вместе с bash-телом). Замен нет: путь через BUNDLE_DIR; wg - заглушка в PATH."""
    results = []
    os.umask(0o022)
    for kind in ('bash', 'py'):
        root = tmp_path / kind
        wg = make_wg(root, CONFS[name])
        (wg / 'server.pub').symlink_to('/nonexistent/pub')
        stubs = tmp_path / f'stubs-{kind}'
        stub(stubs, 'wg', WG_STUB)
        log = tmp_path / f'{kind}.log'
        if kind == 'bash':
            body = block('WG_BUNDLE=', '# Стадия 30 ждёт')
            code, err = bash(f'BUNDLE_DIR={root}\n{body}', {'WGLOG': str(log)}, str(stubs))
        else:
            old = dict(os.environ)
            os.environ.update(WGLOG=str(log), PATH=f'{stubs}:{old["PATH"]}')
            try:
                prepare.wg_key_material(root)
                code, err = 0, ''
            except RestoreError as exc:
                code, err = exc.code, exc.message + '\n'
            finally:
                os.environ.clear()
                os.environ.update(old)
        results.append((code, err, snapshot_dir(wg), log.read_bytes() if log.exists() else b''))
    assert results[0][0] == results[1][0]
    assert results[0][2] == results[1][2]
    assert results[0][3] == results[1][3]
    assert results[0][1].strip() == results[1][1].strip()


def test_wg_key_values_and_stdin_not_argv(tmp_path):
    wg = make_wg(tmp_path, CONFS['quirky'])
    seen = {}

    def run(args, **kw):
        seen['args'], seen['stdin'] = args, kw['stdin'].read()
        kw['stdout'].write(b'PUB\n')
        return SimpleNamespace(returncode=0)

    prepare.wg_key_material(tmp_path, run=run)
    assert seen == {'args': ['wg', 'pubkey'], 'stdin': b'BB=\n'}
    assert (wg / 'server.priv').read_bytes() == b'BB=\n'
    assert (wg / 'server.pub').read_bytes() == b'PUB\n'
    assert stat.S_IMODE((wg / 'server.priv').stat().st_mode) == 0o600
    assert stat.S_IMODE((wg / 'server.pub').stat().st_mode) == 0o600


@pytest.mark.parametrize('make_run', ['rc', 'oserror'])
def test_wg_failure_code_5_without_key_in_text(tmp_path, make_run):
    make_wg(tmp_path, b'[Interface]\nPrivateKey=SECRETVALUE\n')

    def run(_args, **_kw):
        if make_run == 'oserror':
            raise FileNotFoundError('wg')
        return SimpleNamespace(returncode=1)

    with pytest.raises(RestoreError) as err:
        prepare.wg_key_material(tmp_path, run=run)
    assert err.value.code == 5
    assert 'SECRETVALUE' not in err.value.message
    assert err.value.message == 'anysda-restore: неверный или отсутствующий приватный ключ WG в архиве'


def test_wg_noop_and_dangling_link(tmp_path):
    prepare.wg_key_material(tmp_path / 'нет набора')
    wg = make_wg(tmp_path, None)
    (wg / 'server.priv').symlink_to('/nonexistent')
    with pytest.raises(RestoreError) as err:
        prepare.wg_key_material(tmp_path)
    assert err.value.code == 5
    assert 'ссылка на ключ WG без wg0.conf' in err.value.message


def test_wg_existing_priv_kept_and_repeat(tmp_path):
    wg = make_wg(tmp_path, CONFS['plain'], [('server.priv', b'KEEP\n')])
    runs = []

    def run(_args, **kw):
        runs.append(kw['stdin'].read())
        return SimpleNamespace(returncode=0)

    prepare.wg_key_material(tmp_path, run=run)
    prepare.wg_key_material(tmp_path, run=run)
    assert runs == [b'KEEP\n', b'KEEP\n']
    assert (wg / 'server.priv').read_bytes() == b'KEEP\n'


# --- config_template -------------------------------------------------------

def test_config_template_copy_modes_and_repeat(tmp_path):
    tpl = tmp_path / 'src/infra/configs/anysda-config.yaml.tpl'
    tpl.parent.mkdir(parents=True)
    tpl.write_bytes('шаблон {{x}}\n'.encode())
    dest = tmp_path / 'tmp' / 'anysda'
    result = prepare.config_template(tmp_path / 'src', dest_dir=dest)
    assert result == dest / 'anysda-config.yaml.tpl'
    assert result.read_bytes() == tpl.read_bytes()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    tpl.write_bytes(b'new')
    assert prepare.config_template(tmp_path / 'src', dest_dir=dest).read_bytes() == b'new'


def test_config_template_missing_code_5(tmp_path):
    with pytest.raises(RestoreError) as err:
        prepare.config_template(tmp_path / 'пусто', dest_dir=tmp_path / 'd')
    assert err.value.code == 5
    assert err.value.message == (f'anysda-restore: нет шаблона {tmp_path}/пусто/infra/configs/'
                                 'anysda-config.yaml.tpl — данные не изменены')
    assert not (tmp_path / 'd').exists()


# --- guards ----------------------------------------------------------------

def make_db(path, clients):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE clients (id INTEGER)')
        db.executemany('INSERT INTO clients VALUES (?)', [(i,) for i in range(clients)])


@SQLITE
@pytest.mark.parametrize(('clients', 'force', 'caddy_dir', 'has_caddy'), [
    (0, 0, False, True), (2, 0, False, True), (2, 1, False, True), (0, 1, True, False),
    (2, 0, True, False), (2, 0, True, True), (None, 0, False, True),
])
def test_guards_match_bash_block(tmp_path, clients, force, caddy_dir, has_caddy):
    """Прямая сверка с блоком 368-387 (удалить вместе с bash-тела). Замены: LIVE_DB=путь в tmp_path; id - заглушка."""
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    if caddy_dir:
        (bundle / 'caddy-storage').mkdir()
    db = tmp_path / 'db.sqlite'
    if clients is not None:
        make_db(db, clients)
    stubs = tmp_path / 'stubs'
    stub(stubs, 'id', f'exit {0 if has_caddy else 1}\n')
    body = block('CADDY_STORAGE=', '# ── Pre-restore').replace('/var/lib/anysda-vpn2/db.sqlite', str(db))
    code, err = bash(f'BUNDLE_DIR={bundle}\nFORCE={force}\n{body}', path=str(stubs))
    old = os.environ['PATH']
    os.environ['PATH'] = f'{stubs}:{old}'
    try:
        prepare.guards(force=bool(force), bundle_dir=bundle, db_path=db)
        got = (0, '')
    except RestoreError as exc:
        got = (exc.code, exc.message + '\n')
    finally:
        os.environ['PATH'] = old
    assert got == (code, err)


def test_guards_literals_and_failures(tmp_path):
    db = tmp_path / 'db.sqlite'
    db.write_bytes(b'x' * 7)

    def counting(value, rc=0):
        return lambda _a, **_k: SimpleNamespace(returncode=rc, stdout=value)

    with pytest.raises(RestoreError) as err:
        prepare.guards(force=False, bundle_dir=tmp_path, db_path=db, run=counting(b'3\n'))
    assert err.value.code == 6
    assert err.value.message.split('\n')[0] == 'anysda-restore: ABORT — текущая db.sqlite не пустая (3 клиентов, 7 байт).'
    prepare.guards(force=True, bundle_dir=tmp_path, db_path=db, run=counting(b'3\n'))
    prepare.guards(force=False, bundle_dir=tmp_path, db_path=db, run=counting(b'', rc=1))
    prepare.guards(force=False, bundle_dir=tmp_path, db_path=db, run=counting('мусор'.encode()))
    prepare.guards(force=False, bundle_dir=tmp_path, db_path=tmp_path / 'нет')
    db.write_bytes(b'')
    prepare.guards(force=False, bundle_dir=tmp_path, db_path=db, run=counting(b'5'))

    def broken(_a, **_k):
        raise FileNotFoundError('sqlite3')

    prepare.guards(force=False, bundle_dir=tmp_path, db_path=db, run=broken)
    (tmp_path / 'caddy-storage').mkdir()
    with pytest.raises(RestoreError) as err:
        prepare.guards(force=True, bundle_dir=tmp_path, db_path=db, run=broken)
    assert err.value.code == 5


# --- pre_restore_snapshot --------------------------------------------------

SCRIPT_PATHS = [
    'etc/anysda', 'etc/wireguard', 'etc/openvpn', 'etc/strongswan', 'etc/swanctl', 'etc/strongswan.conf',
    'etc/strongswan.d', 'etc/ipsec.conf', 'etc/ipsec.secrets', 'etc/caddy', 'var/lib/caddy/.local/share/caddy',
    'var/lib/anysda-vpn2/runtime', 'var/lib/vpn2-control', 'opt/AdGuardHome/AdGuardHome.yaml', 'var/anysda/.stamps',
]
TS = '2026-01-02T030405.123456789Z'


def fake_pty(log, produce=True):
    def run(argv, passphrase, prompts, *, timeout):
        log.append((argv, passphrase, prompts, timeout))
        if produce:
            Path(argv[-2]).write_bytes(b'AGE:' + Path(argv[-1]).read_bytes())
        return 0
    return run


def make_node(tmp_path):
    node = tmp_path / 'node'
    (node / 'etc/anysda').mkdir(parents=True)
    (node / 'etc/anysda/a.conf').write_text('a')
    (node / 'etc/wireguard').mkdir()
    (node / 'etc/wireguard/wg0.conf').write_text('w')
    (node / 'etc/caddy').symlink_to('anysda')
    os.mkfifo(node / 'etc/anysda/fifo')
    live = node / 'var/lib/anysda-vpn2/db.sqlite'
    live.parent.mkdir(parents=True)
    make_db(live, 2)
    return node, live


def test_snapshot_paths_equal_script_list():
    assert list(prepare.SNAPSHOT_PATHS) == SCRIPT_PATHS
    if HAS_BASH:  # удалить вместе с bash-телом
        text = block('for path in \\', 'find "$PRESNAP_ROOT"')
        assert all(p in text for p in SCRIPT_PATHS)


@SQLITE
def test_snapshot_end_to_end(tmp_path):
    node, live = make_node(tmp_path)
    settings = SimpleNamespace(local_dir=str(tmp_path / 'backups'), passphrase='пароль')
    work = tmp_path / 'work'
    work.mkdir()
    calls, log, said = [], [], []

    def run(args, **kw):
        calls.append(args[0])
        return subprocess.run(args, **kw)

    result = prepare.pre_restore_snapshot(settings, TS, work=work, root=node, live_db=live, run=run,
                                          pty=fake_pty(log), out=said.append)
    final = tmp_path / 'backups/.pre-restore' / f'pre-restore-{TS}.tar.gz.age'
    assert result == final
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert stat.S_IMODE(final.parent.stat().st_mode) == 0o700
    assert said == [f'anysda-restore: pre-restore snapshot → {final}',
                    f'anysda-restore: pre-restore snapshot saved ({final.stat().st_size} bytes)']
    assert calls == ['sqlite3', 'cp', 'cp', 'cp', 'find', 'tar']
    argv, passphrase, _prompts, _t = log[0]
    assert argv == ['age', '-p', '-o', str(work / 'pre-restore.tar.gz.age'), str(work / f'pre-restore-{TS}.tar.gz')]
    assert passphrase == 'пароль'
    assert not (work / f'pre-restore-{TS}.tar.gz').exists()
    assert not (work / 'pre-restore.tar.gz.age').exists()
    tar_bytes = final.read_bytes()[4:]
    tar_file = tmp_path / 'out.tar.gz'
    tar_file.write_bytes(tar_bytes)
    with tarfile.open(tar_file) as tar:
        names = sorted(tar.getnames())
    assert names == ['.', './etc', './etc/anysda', './etc/anysda/a.conf', './etc/caddy', './etc/wireguard',
                     './etc/wireguard/wg0.conf', './var', './var/lib', './var/lib/anysda-vpn2',
                     './var/lib/anysda-vpn2/db.sqlite']


@SQLITE
def test_snapshot_db_copy_is_standalone_and_repeat(tmp_path):
    node, live = make_node(tmp_path)
    settings = SimpleNamespace(local_dir=str(tmp_path / 'b'), passphrase='p')
    work = tmp_path / 'w'
    work.mkdir()
    for ts in (TS, TS + '2'):
        prepare.pre_restore_snapshot(settings, ts, work=work, root=node, live_db=live, pty=fake_pty([]), out=lambda _t: None)
    assert len(list((tmp_path / 'b/.pre-restore').iterdir())) == 2


def test_snapshot_errors(tmp_path):
    settings = SimpleNamespace(local_dir=str(tmp_path / 'b'), passphrase='p')
    work = tmp_path / 'w'
    work.mkdir()
    node = tmp_path / 'node'
    node.mkdir()
    common = {'work': work, 'root': node, 'live_db': node / 'нет.db', 'out': lambda _t: None}
    with pytest.raises(RestoreError) as err:
        prepare.pre_restore_snapshot(settings, TS, pty=fake_pty([], produce=False), **common)
    assert (err.value.code, err.value.message) == (5, 'anysda-restore: пустой страховочный архив')
    assert not list((tmp_path / 'b/.pre-restore').iterdir())

    def tar_fails(args, **kw):
        if args[0] == 'tar':
            return SimpleNamespace(returncode=2, stderr=b'')
        return subprocess.run(args, **kw)

    with pytest.raises(RestoreError) as err:
        prepare.pre_restore_snapshot(settings, TS, run=tar_fails, pty=fake_pty([]), **common)
    assert err.value.code == 2

    def missing(args, **_kw):
        raise FileNotFoundError(args[0])

    with pytest.raises(RestoreError) as err:
        prepare.pre_restore_snapshot(settings, TS, run=missing, pty=fake_pty([]), **common)
    assert err.value.code == 127


def test_snapshot_sqlite_and_cp_failures(tmp_path):
    node, live = make_node(tmp_path)
    settings = SimpleNamespace(local_dir=str(tmp_path / 'b'), passphrase='p')
    (tmp_path / 'w').mkdir()
    for tool in ('sqlite3', 'cp'):
        def run(args, tool=tool, **kw):
            if args[0] == tool:
                return SimpleNamespace(returncode=9, stderr=b'')
            return subprocess.run(args, **kw)

        with pytest.raises(RestoreError) as err:
            prepare.pre_restore_snapshot(settings, TS, work=tmp_path / 'w', root=node, live_db=live, run=run,
                                         pty=fake_pty([]), out=lambda _t: None)
        assert err.value.code == 9


def test_snapshot_encrypt_error_type(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise ArchiveError(3, 'x')

    monkeypatch.setattr(prepare.archive, 'encrypt', boom)
    (tmp_path / 'w').mkdir()
    with pytest.raises(RestoreError) as err:
        prepare.pre_restore_snapshot(SimpleNamespace(local_dir=str(tmp_path / 'b'), passphrase='p'), TS,
                                     work=tmp_path / 'w', root=tmp_path, live_db=tmp_path / 'нет', out=lambda _t: None)
    assert err.value.code == 5
