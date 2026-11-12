"""Полный ход restore на поддельном узле: архив снимает vpn2.backup.run, восстанавливает vpn2.backup.restore."""
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_backup_run import STUBS, create, make_node, put, values, write_env
from vpn2.backup import restore

needs_tools = pytest.mark.skipif(
    any(shutil.which(name) is None for name in ('bash', 'sqlite3', 'tar', 'shred')),
    reason='нужны bash, sqlite3, tar, shred')
# Заглушки: argv в журнал; отказ по шаблону FAIL_ON; install без -o/-g (без root) - настоящий.
STUB = '''#!/bin/sh
n=$(basename "$0")
echo "$n $*" >> "$T/calls.log"
case "$n $*" in $FAIL_ON) echo "$n: отказ" >&2; exit 1 ;; esac
case "$n" in
  id) case " $IDS " in *" $1 "*) exit 0 ;; esac; exit 1 ;;
  systemctl) case "$1" in
    is-active) case " $ACTIVE " in *" $3 "*) exit 0 ;; esac; exit 3 ;;
    cat) case " $UNITS " in *" $2 "*) exit 0 ;; esac; exit 1 ;;
  esac ;;
  docker) [ "$*" = "container inspect anysda-tgbot" ] && exit 1 ;;
  install) skip=
    for a; do shift; if [ -n "$skip" ]; then skip=; continue; fi
      case "$a" in -o|-g) skip=1; continue ;; esac; set -- "$@" "$a"; done
    exec @INSTALL@ "$@" ;;
esac
exit 0
'''
STAGE = '''#!/bin/sh
case "$1 $2" in
  'adguard config') n=22-adguard ;; 'adguard restart') exit 0 ;; 'containers panel') n=30-frontend ;; *) n=35-telegram ;;
esac
echo "stage $n" >> "$T/calls.log"
echo "AGH_PORT=$AGH_PORT HOST=$HOST ADMIN_PASSWORD=${ADMIN_PASSWORD-unset} VPN2_DEVELOPMENT=$VPN2_DEVELOPMENT" > "$T/$n.env"
echo "вывод $n"
[ ! -f "$T/rc-$n" ] || exit "$(cat "$T/rc-$n")"
'''
VERIFY = '''import os
import sys

t = os.environ['T']
with open(t + '/calls.log', 'a') as log:
    log.write('stage 99-verify\\n')
rc = t + '/rc-99-verify'
sys.exit(int(open(rc).read()) if os.path.exists(rc) else 0)
'''
WG = '#!/bin/sh\n[ "$1" = pubkey ] && { cat > /dev/null; echo PUBKEY; }\nexit 0\n'
UNITS = 'sing-box openvpn-server@server strongswan-starter'


@pytest.fixture
def node(tmp_path, monkeypatch, capsys):
    """Узел с архивом: после съёма бэкапа живые файлы изменены, restore должен вернуть архивные."""
    root = tmp_path / 'n'
    root.mkdir()
    paths = make_node(root)
    conn = sqlite3.connect(paths.db)
    conn.execute('create table __drizzle_migrations (created_at, hash)')
    conn.execute('create table clients (id)')
    conn.commit()
    conn.close()
    src = tmp_path / 'src'
    put(src / 'app/server/database/migrations/meta/_journal.json', '{"entries": []}')
    put(src / 'infra/configs/anysda-config.yaml.tpl', 'tpl\n')
    put(src / 'infra/ansible/inventory.py', "print('AGH_PORT=3000\\nSSH_PASS=x\\nHOST=ru')\n")
    for name, body in (('__init__.py', ''), ('cli/__init__.py', ''), ('verify/stage.py', ''),
                       ('cli/__main__.py', VERIFY)):
        put(src / 'infra/vpn2/src/vpn2' / name, body)
    put(Path(paths.source_root_file), f'{src}\n')
    bin_dir = root / 'bin'
    for name, body in STUBS.items():
        put(bin_dir / name, body, 0o755)
    for name in ('systemctl', 'docker', 'chown', 'install', 'id'):
        put(bin_dir / name, STUB.replace('@INSTALL@', shutil.which('install')), 0o755)
    put(bin_dir / 'wg', WG, 0o755)
    put(bin_dir / 'vpn2', STAGE, 0o755)
    monkeypatch.setenv('VPN2_BIN', str(bin_dir / 'vpn2'))
    for key, value in (('PATH', f'{bin_dir}:/usr/bin:/bin'), ('STUB_LOG', str(root / 'log')), ('T', str(tmp_path)),
                       ('FAIL_ON', ''), ('IDS', 'caddy vpn2-panel'), ('UNITS', UNITS), ('ACTIVE', '')):
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('VPN2_DEVELOPMENT', raising=False)
    vals = values(root)
    assert create(root, paths, vals) == 0, capsys.readouterr().err
    name = next((root / 'out dir').glob('anysda-vpn2-*.tar.gz.age')).name
    put(Path(paths.etc_anysda) / 'admin-password', 'changed\n', 0o600)
    put(Path(paths.wireguard) / 'wg0.conf', 'changed\n', 0o600)
    (tmp_path / 'calls.log').unlink(missing_ok=True)
    capsys.readouterr()
    return SimpleNamespace(tmp=tmp_path, root=root, paths=paths, vals=vals, name=name, capsys=capsys)


def go(node, argv=('latest',)):
    code = restore.main(list(argv), root=str(node.root), env_file=node.paths.env_file)
    captured = node.capsys.readouterr()
    return code, captured.out, captured.err


def leftovers(root):
    return list((root / 'var/lib/anysda-vpn2').glob('.restore-tmp.*'))


@needs_tools
def test_restore_returns_archived_state(node):
    tmp, root, paths, vals = node.tmp, node.root, node.paths, node.vals  # noqa: F841
    code, out, err = go(node)
    assert code == 0, err
    assert (Path(paths.etc_anysda) / 'admin-password').read_text() == 'pw\n'
    assert (Path(paths.wireguard) / 'wg0.conf').read_text() == 'wg0.conf\n'
    snaps = list((root / 'out dir/.pre-restore').glob('pre-restore-*.tar.gz.age'))
    assert len(snaps) == 1 and oct(snaps[0].stat().st_mode & 0o777) == '0o600'
    assert out.rstrip('\n').endswith(
        '\nanysda-restore: SUCCESS\n'
        f'  source:       {root}/out dir/{node.name}\n'
        f'  pre-snapshot: {snaps[0]}\n'
        '  Существующие клиенты должны переподключаться без переоформления конфигов.')
    for line in ('manifest verified', 'схема БД архива: 0 из 0', '22-adguard re-synced', '35-telegram re-synced',
                 '99-verify PASSED', 'останавливаю сервисы…', 'запускаю сервисы…'):
        assert line in out, line
    calls = (tmp / 'calls.log').read_text()
    order = [calls.index(mark) for mark in ('docker stop anysda-vpn2', 'docker start anysda-vpn2',
                                            'stage 22-adguard', 'stage 30-frontend', 'stage 35-telegram',
                                            'stage 99-verify')]
    assert order == sorted(order)
    assert (tmp / '30-frontend.env').read_text() == 'AGH_PORT=3000 HOST=ru ADMIN_PASSWORD=unset VPN2_DEVELOPMENT=false\n'
    assert (root / 'tmp/anysda/anysda-config.yaml.tpl').read_text() == 'tpl\n'
    assert not leftovers(root)


@needs_tools
def test_dry_run_changes_nothing(node):
    tmp, root, paths, vals = node.tmp, node.root, node.paths, node.vals  # noqa: F841
    code, out, _err = go(node, ['--dry-run'])
    assert (code, out) == (0, f'anysda-restore: dry-run — восстановил бы {node.name} '
                              '(local), ничего не изменено\n')
    assert (Path(paths.etc_anysda) / 'admin-password').read_text() == 'changed\n'
    assert not (tmp / 'calls.log').exists() and not leftovers(root)


@needs_tools
def test_failed_stage_is_code_8_and_stops_chain(node):
    tmp, root, paths, vals = node.tmp, node.root, node.paths, node.vals  # noqa: F841
    put(tmp / 'rc-30-frontend', '5')
    code, out, err = go(node)
    assert code == 8
    assert 'SUCCESS' not in out and '99-verify PASSED' in out
    assert 'stage 35-telegram' not in (tmp / 'calls.log').read_text()
    assert ('anysda-restore: ERROR: пароли не пересинхронизированы — 30-frontend завершился с ошибкой, '
            'хвост вывода:\n    вывод 30-frontend\n') in err
    assert err.rstrip('\n').endswith('Почини причину и прогони ./deploy.sh <стадия> ru; pre-restore снимок: '
                                     + str(next((root / 'out dir/.pre-restore').iterdir())))
    assert (Path(paths.etc_anysda) / 'admin-password').read_text() == 'pw\n'


@needs_tools
@pytest.mark.parametrize(('verify_rc', 'code', 'text'), [
    ('1', 7, 'anysda-restore: 99-verify FAILED — проверь руками; pre-restore снимок: '),
    ('2', 0, 'anysda-restore: ⚠ 99-verify: не все проверки измерены; причины выше\n'),
])
def test_verify_exit_codes(node, verify_rc, code, text):
    tmp, root, paths, vals = node.tmp, node.root, node.paths, node.vals  # noqa: F841
    put(tmp / 'rc-99-verify', verify_rc)
    got, out, err = go(node)
    assert got == code and text in err
    assert ('SUCCESS' in out) == (code == 0)
    assert not leftovers(root)


@needs_tools
def test_failed_step_rolls_back_with_code_10(node, monkeypatch):
    tmp, root, paths, vals = node.tmp, node.root, node.paths, node.vals  # noqa: F841
    monkeypatch.setenv('FAIL_ON', 'systemctl start openvpn-server@server')
    code, out, err = go(node)
    assert code == 10
    assert 'anysda-restore: FAILED на шаге «запуск службы openvpn-server@server»: systemctl start openvpn-server@server (код 1)' in err
    assert 'откат после сбоя' not in err and 'SUCCESS' not in out
    assert (Path(paths.etc_anysda) / 'admin-password').read_text() == 'changed\n'
    assert (Path(paths.wireguard) / 'wg0.conf').read_text() == 'changed\n'
    assert 'stage' not in (tmp / 'calls.log').read_text()
    assert not leftovers(root)


@needs_tools
def test_refusals_before_any_change(node, monkeypatch):
    tmp, root, paths, vals = node.tmp, node.root, node.paths, node.vals  # noqa: F841
    assert go(node, ['latest', '--bogus'])[::2] == (2, 'anysda-restore: неизвестный аргумент: --bogus\n')
    code, _out, err = go(node, ['/any/dir/anysda-vpn2-2020-01-01T000000Z.tar.gz.age'])
    assert (code, err) == (3, f'anysda-restore: архив anysda-vpn2-2020-01-01T000000Z.tar.gz.age не найден в '
                              f'{root}/out dir\n')
    conn = sqlite3.connect(paths.db)
    conn.execute('insert into clients values (1)')
    conn.commit()
    conn.close()
    code, _out, err = go(node)
    assert code == 6 and 'ABORT — текущая db.sqlite не пустая (1 клиентов' in err
    monkeypatch.setenv('STUB_AGE_FAIL', '1')
    code, _out, err = go(node, ['latest', '--force'])
    assert code == 4 and 'дешифровка провалилась (age exit=1)' in err
    write_env(root, {**vals, 'ANYSDA_BACKUP_PASSPHRASE': ''})
    code, _out, err = go(node)
    assert (code, err) == (2, f'anysda-restore FATAL: ANYSDA_BACKUP_PASSPHRASE пуст в {paths.env_file}.\n'
                              '  Заполни backup.passphrase в config.yaml и прогони ./deploy.sh 26-backup ru\n')
    os.unlink(paths.env_file)
    assert go(node)[::2] == (2, f'anysda-restore: {paths.env_file} не доступен\n')
    assert (Path(paths.etc_anysda) / 'admin-password').read_text() == 'changed\n'
    calls = (tmp / 'calls.log').read_text()
    assert 'systemctl' not in calls and 'docker stop' not in calls and not leftovers(root)


@needs_tools
def test_s3_backend_without_keys_restores_local_archive(node):
    """Скрипт спрашивал переменные S3 только при обращении к облаку: архив уже лежит рядом - они не нужны."""
    tmp, root, paths, vals = node.tmp, node.root, node.paths, node.vals  # noqa: F841
    write_env(root, {**vals, 'BACKUP_BACKEND': 's3'})
    name = node.name
    assert go(node, [name, '--dry-run'])[:2] == (
        0, f'anysda-restore: dry-run — восстановил бы {name} (local), ничего не изменено\n')
    code, _out, err = go(node, ['latest', '--dry-run'])
    assert code == 0 and '  BACKUP_S3_BUCKET: parameter null or not set\n' in err
    code, _out, err = go(node, ['anysda-vpn2-2020-01-01T000000Z.tar.gz.age'])
    assert (code, err) == (1, 'anysda-restore: BACKUP_S3_BUCKET: parameter null or not set\n')


@needs_tools
def test_preflight_refuses_like_the_real_run_and_touches_nothing(node, monkeypatch):
    """Проход деплоя перед открытием выпуска: те же код и строка отказа, узел не тронут."""
    tmp, root, paths = node.tmp, node.root, node.paths

    def untouched():
        calls = (tmp / 'calls.log').read_text() if (tmp / 'calls.log').exists() else ''
        return ((Path(paths.etc_anysda) / 'admin-password').read_text() == 'changed\n' and 'systemctl' not in calls
                and 'docker stop' not in calls and not leftovers(root) and not (root / 'tmp/anysda').exists()
                and not list((root / 'out dir/.pre-restore').glob('*')))
    code, out, err = go(node, ['latest', '--preflight'])
    assert code == 0, err
    assert 'SUCCESS' not in out and untouched()
    conn = sqlite3.connect(paths.db)
    conn.execute('insert into clients values (1)')
    conn.commit()
    conn.close()
    code, _out, err = go(node, ['latest', '--preflight'])
    assert code == 6 and untouched()
    monkeypatch.setenv('STUB_AGE_FAIL', '1')
    assert go(node, ['latest', '--force', '--preflight'])[0] == 4 and untouched()
    monkeypatch.delenv('STUB_AGE_FAIL')
    assert (code, err) == go(node)[::2]
