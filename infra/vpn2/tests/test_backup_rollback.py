"""Откат restore: Rollback против блока copy_into/rollback_path/rollback из anysda-restore.sh."""
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from vpn2.backup.errors import RestoreError
from vpn2.backup.rollback import Rollback, StepFailed

RESTORE = Path(__file__).resolve().parent / 'fixtures' / 'backup' / 'anysda-restore.sh'
SRC = Path(__file__).resolve().parents[1] / 'src'
needs_bash = pytest.mark.skipif(shutil.which('bash') is None, reason='нужен bash')

STUB = '''#!/bin/sh
echo "$(basename "$0") $*" >> "$T/calls.log"
if [ -n "${INT_ON:-}" ] && [ "$*" = "$INT_ON" ] && [ ! -e "$T/int-sent" ]; then
  : > "$T/int-sent"; kill -INT "$PPID"
fi
case "$(basename "$0") $*" in
  "systemctl start sing-box") echo "sing-box: start failed" >&2; exit 1 ;;
  "docker container inspect anysda-tgbot") exit 1 ;;
esac
exit 0
'''


def node(t):
    """Корень узла, бандл и страховочная копия: файл, каталог, путь, которого до restore не было."""
    root, bundle, presnap = t / 'root', t / 'bundle', t / 'presnap'
    for base, text, mode in ((root, 'old', 0o640), (bundle, 'new', 0o600)):
        (base / 'srv' / 'dir' / 'sub').mkdir(parents=True)
        (base / 'srv' / 'conf.txt').write_text(f'{text} conf\n')
        os.chmod(base / 'srv' / 'conf.txt', mode)
        (base / 'srv' / 'dir' / 'sub' / 'a b.txt').write_text(f'{text} файл\n')
        os.symlink('sub/a b.txt', base / 'srv' / 'dir' / 'link')
    os.chmod(root / 'srv' / 'dir', 0o750)
    (bundle / 'srv' / 'new').mkdir()
    (bundle / 'srv' / 'new' / 'n.txt').write_text('new\n')
    (root / 'srv' / 'dir' / 'only-old').write_text('x\n')
    presnap.mkdir()
    subprocess.run(['cp', '-a', str(root / 'srv'), str(presnap)], check=True)
    (t / 'work').mkdir()
    bin_dir = t / 'bin'
    bin_dir.mkdir()
    for name in ('systemctl', 'docker'):
        (bin_dir / name).write_text(STUB)
        os.chmod(bin_dir / name, 0o755)


def tree(base):
    """Дерево: путь, тип, права, содержимое или цель ссылки."""
    out = []
    for dirpath, dirs, files in os.walk(base):
        for name in sorted(dirs + files):
            p = Path(dirpath) / name
            st = p.lstat()
            body = os.readlink(p) if p.is_symlink() else (p.read_text() if p.is_file() else '')
            out.append((str(p.relative_to(base)), oct(st.st_mode), body))
    return sorted(out)


def bash_block():
    """Функции отката из скрипта; пути узла - под $ROOT. Удалить вместе с bash-телом."""
    text = RESTORE.read_text()
    start = text.index('copy_into() {')
    mid = text.index('rollback() {', start)
    end = text.index('# ── Остановка сервисов', mid)
    paths = text[start:mid].replace('"/$', '"$ROOT/$')
    rest = text[mid:end].replace(' /var/lib/', ' $ROOT/var/lib/').replace(' /etc/', ' $ROOT/etc/')
    return paths + rest


BASH_RUN = '''set -euo pipefail
ROOT=$T/root PRESNAP_ROOT=$T/presnap PRESNAP_FINAL=$T/presnap.tar.gz.age WORK=$T/work
BUNDLE_DIR=$T/bundle CADDY_SYNC_UNITS=() LIVE_DB_MODE=600 LIVE_DB_OWNER=0:0
STEP="остановка служб" TOUCHED=() DB_SWAPPED=0 CONTROL_STOPPED=0
{block}
STEP="файл"; TOUCHED+=(srv/conf.txt); cp -a "$BUNDLE_DIR/srv/conf.txt" "$ROOT/srv/conf.txt"
STEP="каталог"; TOUCHED+=(srv/dir); copy_into "$BUNDLE_DIR/srv/dir" "$ROOT/srv/dir"
case $MODE in fail|int) start_unit sing-box ;; term) kill -TERM $$ ;; esac
STEP="новый"; TOUCHED+=(srv/new); copy_into "$BUNDLE_DIR/srv/new" "$ROOT/srv/new"
trap - ERR INT TERM
echo DONE
'''

PY_RUN = '''import os, signal, subprocess, sys
from vpn2.backup.errors import RestoreError
from vpn2.backup.rollback import Rollback
T, MODE = os.environ['T'], os.environ['MODE']
rb = Rollback(presnap_root=T + '/presnap', presnap_final=T + '/presnap.tar.gz.age',
              work=T + '/work', bundle_dir=T + '/bundle', root=T + '/root')
try:
    with rb:
        rb.step = 'файл'
        rb.touch('srv/conf.txt')
        subprocess.run(['cp', '-a', T + '/bundle/srv/conf.txt', T + '/root/srv/conf.txt'], check=True)
        rb.step = 'каталог'
        rb.touch('srv/dir')
        rb.copy_into(T + '/bundle/srv/dir', T + '/root/srv/dir')
        if MODE in ('fail', 'int'):
            rb.start_unit('sing-box')
        if MODE == 'term':
            os.kill(os.getpid(), signal.SIGTERM)
        rb.step = 'новый'
        rb.touch('srv/new')
        rb.copy_into(T + '/bundle/srv/new', T + '/root/srv/new')
except RestoreError as e:
    sys.exit(e.code)
print('DONE')
'''


def scenario(t, mode, argv):
    """Узел в t, прогон argv с заглушками в PATH: код, stdout, stderr, журнал, дерево корня."""
    node(t)
    env = dict(os.environ, T=str(t), MODE=mode, PATH=f'{t}/bin:{os.environ["PATH"]}',
               PYTHONPATH=str(SRC), INT_ON='cat openvpn-server@server' if mode == 'int' else '')
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
    calls = (t / 'calls.log').read_text() if (t / 'calls.log').exists() else ''
    norm = (lambda s: s.replace(str(t), 'T'))
    return done.returncode, norm(done.stdout), norm(done.stderr), calls, tree(t / 'root')


@needs_bash
@pytest.mark.parametrize('mode', ['ok', 'fail', 'term', 'int'])
def test_rollback_matches_bash(tmp_path, mode):
    """Прямая сверка с bash: успех, ошибка, TERM после второй замены, INT посреди отката.

    Удалить вместе с bash-телом.
    """
    old = scenario(tmp_path / 'bash', mode, ['bash', '-c', BASH_RUN.format(block=bash_block())])
    new = scenario(tmp_path / 'py', mode, [sys.executable, '-c', PY_RUN])
    assert new == old
    assert old[0] == (0 if mode == 'ok' else 10)


class Fake:
    """run: systemctl и docker - в журнал с кодом из fail, остальное - настоящая программа."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, argv, **kw):
        if argv[0] in ('systemctl', 'docker'):
            self.calls.append(' '.join(argv))
            return subprocess.CompletedProcess(argv, 1 if ' '.join(argv) in self.fail else 0)
        return subprocess.run(argv, **kw)


def make(t, run, **kw):
    return Rollback(presnap_root=t / 'presnap', presnap_final=t / 'snap.age', work=t / 'work',
                    bundle_dir=t / 'bundle', root=t / 'root', run=run, **kw)


def test_openvpn_abort_requirements(tmp_path, capsys):
    """Как test_openvpn_good_restore_abort: отказ start после замены /etc/openvpn - код 10 и возврат."""
    live = tmp_path / 'root/etc/openvpn/server'
    live.mkdir(parents=True)
    (live / 'server.conf').write_text('port 1195\n')
    (tmp_path / 'presnap/etc').mkdir(parents=True)
    subprocess.run(['cp', '-a', str(tmp_path / 'root/etc/openvpn'), str(tmp_path / 'presnap/etc')], check=True)
    (tmp_path / 'bundle/etc-openvpn/server').mkdir(parents=True)
    (tmp_path / 'bundle/etc-openvpn/server/server.conf').write_text('port 1194\n')
    rb = make(tmp_path, Fake(fail={'systemctl start strongswan-starter'}))
    with pytest.raises(RestoreError) as caught, rb:
        rb.touch('etc/openvpn')
        rb.copy_into(tmp_path / 'bundle/etc-openvpn', tmp_path / 'root/etc/openvpn')
        rb.start_unit('openvpn-server@server')
        rb.start_unit('strongswan-starter')
    err = capsys.readouterr().err
    assert caught.value.code == 10
    assert 'anysda-restore: FAILED на шаге «запуск службы strongswan-starter»' in err
    assert 'возвращено из страховочной копии: /etc/openvpn\n' in err
    assert 'не запустились: strongswan-starter\n' in err
    assert (live / 'server.conf').read_text() == 'port 1195\n'


def test_empty_rollback(tmp_path, capsys):
    """Пустой учёт: ничего не заменялось, службы запускаются, код 10."""
    (tmp_path / 'bundle').mkdir()
    fake = Fake()
    with pytest.raises(RestoreError), make(tmp_path, fake):
        raise OSError('boom')
    err = capsys.readouterr().err.splitlines()
    assert err == ['anysda-restore: FAILED на шаге «остановка служб»: boom (код 1) — возвращаю прежнее состояние',
                   'anysda-restore: возвращено из страховочной копии: ничего не заменялось',
                   'anysda-restore: службы и контейнеры прежнего состояния запущены',
                   f'anysda-restore: страховочный снимок: {tmp_path}/snap.age']
    assert fake.calls[0] == 'systemctl cat sing-box' and fake.calls[-1] == 'docker start anysda-tgbot'


def test_rollback_failures_do_not_stop_rest(tmp_path, capsys):
    """Отказ start и сбой пути в откате: остальные шаги идут, всё названо в отчёте."""
    (tmp_path / 'bundle/caddy-storage').mkdir(parents=True)
    (tmp_path / 'root/etc/wireguard').mkdir(parents=True)
    (tmp_path / 'root/etc/wireguard/wg0.conf').write_text('[Interface]\n')
    fake = Fake(fail={'systemctl start sing-box', 'systemctl start caddy'})
    rb = make(tmp_path, fake)
    rb.stopped('vpn2-control')
    rb.stopped('anysda-ikev2-cert-sync.path')
    rb.touch('etc/x', 'etc/x', 'db')
    rb.rollback_path = lambda _rel: 1 / 0
    rb.rollback(1, 'false')
    rb.rollback(1, 'false')
    err = capsys.readouterr().err
    assert err.count('FAILED') == 1
    assert 'возвращено из страховочной копии: /var/lib/anysda-vpn2/db.sqlite\n' in err
    assert f'НЕ возвращено: /etc/x — бери из {tmp_path}/snap.age\n' in err
    assert 'не запустились: sing-box caddy\n' in err
    for call in ('systemctl restart wg-quick@wg0', 'systemctl start anysda-ikev2-cert-sync.path',
                 'systemctl start vpn2-control.socket vpn2-control.service', 'docker start anysda-tgbot'):
        assert call in fake.calls


def test_db_swapped_back(tmp_path, capsys):
    """Заменённая БД: install из копии с прежним режимом, WAL/SHM убраны, затем mv."""
    db = tmp_path / 'root/var/lib/anysda-vpn2'
    db.mkdir(parents=True)
    (db / 'db.sqlite').write_text('new')
    (db / 'db.sqlite-wal').write_text('w')
    (tmp_path / 'presnap/var/lib/anysda-vpn2').mkdir(parents=True)
    (tmp_path / 'presnap/var/lib/anysda-vpn2/db.sqlite').write_text('old')
    (tmp_path / 'bundle').mkdir()
    rb = make(tmp_path, Fake(), live_db_mode='640', live_db_owner=f'{os.getuid()}:{os.getgid()}')
    rb.touch('db')
    rb.db_swapped = True
    rb.rollback(1, 'x')
    assert (db / 'db.sqlite').read_text() == 'old'
    assert oct((db / 'db.sqlite').stat().st_mode & 0o777) == '0o640'
    assert sorted(os.listdir(db)) == ['db.sqlite']
    assert 'возвращено из страховочной копии: /var/lib/anysda-vpn2/db.sqlite\n' in capsys.readouterr().err


def test_handlers_restored_and_reentry(tmp_path):
    """После выхода обработчики INT/TERM прежние; блок можно открыть снова, но не вложенно."""
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    rb = make(tmp_path, Fake())
    with rb:
        assert signal.getsignal(signal.SIGTERM) is not before[signal.SIGTERM]
        with pytest.raises(RuntimeError), rb:
            pass
    assert {s: signal.getsignal(s) for s in before} == before
    (tmp_path / 'bundle').mkdir()
    with pytest.raises(RestoreError), rb:
        os.kill(os.getpid(), signal.SIGINT)
    assert {s: signal.getsignal(s) for s in before} == before


def test_copy_into_failure_is_step_failed(tmp_path):
    """Сбой cp в copy_into - StepFailed с кодом cp и текстом команды из скрипта."""
    with pytest.raises(StepFailed) as caught:
        make(tmp_path, Fake()).copy_into(tmp_path / 'missing', tmp_path / 'dst')
    assert caught.value.rc == 1 and caught.value.cause == 'cp -a "$src/." "$dst/"'
