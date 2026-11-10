"""Применение набора при restore: apply.py против блоков остановки, применения и старта anysda-restore.sh."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vpn2.backup import apply
from vpn2.backup.rollback import Rollback, StepFailed

RESTORE = Path(__file__).resolve().parent / 'fixtures' / 'backup' / 'anysda-restore.sh'
SRC = Path(__file__).resolve().parents[1] / 'src'
needs_tools = pytest.mark.skipif(shutil.which('bash') is None or shutil.which('sqlite3') is None,
                                 reason='нужны bash и sqlite3')

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
SVC_LIB = 'wg0_apply() { systemctl restart wg-quick@wg0 || return; echo "svc: wg-quick@wg0 перезапущен"; }\n'
VPN2_APPLY = '#!/bin/sh\necho "vpn2-apply $*" >> "$T/calls.log"\nexit 1\n'


def put(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        os.chmod(path, mode)


def sqlite(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['sqlite3', str(path), f"create table t(v); insert into t values('{value}');"], check=True)


def live_node(root, full):
    """Живой узел: БД с WAL, журнал control, /etc/anysda, wg0, ccd, strongSwan, Caddy, svc и vpn2-apply."""
    sqlite(root / 'var/lib/anysda-vpn2/db.sqlite', 'old')
    put(root / 'var/lib/anysda-vpn2/db.sqlite-wal', 'wal\n')
    put(root / 'var/lib/vpn2-control/journal', 'old epoch\n')
    for name, text in (('config.yaml', 'old: 1\n'), ('sso-runtime.json', '{"old": 1}\n'),
                       ('telegram-from-config.json', '{}\n'), ('ikev2-domain', 'vpn.example\n'),
                       ('ikev2-cert.sha256', 'abc\n')):
        put(root / 'etc/anysda' / name, text, 0o640)
    os.chmod(root / 'etc/anysda', 0o750)
    put(root / 'etc/wireguard/wg0.conf', 'old wg\n', 0o600)
    for dev in ('7', '9'):
        put(root / f'etc/openvpn/server/ccd/dev_{dev}', f'ifconfig-push {dev}\n')
    put(root / 'etc/strongswan/old.conf', 'old\n')
    put(root / 'etc/ipsec.conf', 'old ipsec\n')
    put(root / 'var/lib/caddy/.local/share/caddy/certs/old.crt', 'old cert\n')
    if full:
        put(root / 'usr/local/lib/anysda/anysda-svc.sh', SVC_LIB)
        put(root / 'usr/local/sbin/vpn2-apply', VPN2_APPLY, 0o755)


def bundle_tree(bundle, full):
    """Набор: полный - все компоненты (имя с пробелом и не-ASCII), иначе только etc-anysda."""
    put(bundle / 'etc-anysda/config.yaml', 'new: 1\n', 0o600)
    put(bundle / 'etc-anysda/мой файл.txt', 'новое\n')
    if not full:
        return
    put(bundle / 'etc-anysda/source-root', 'other host\n')
    put(bundle / 'etc-anysda/telegram-runtime.json', '{"tg": 1}\n', 0o600)
    sqlite(bundle / 'db.sqlite', 'new')
    put(bundle / 'etc-wireguard/wg0.conf', 'new wg\n', 0o644)
    put(bundle / 'etc-wireguard/server.priv', 'priv\n', 0o600)
    put(bundle / 'etc-wireguard/server.pub', 'pub\n', 0o644)
    put(bundle / 'etc-openvpn/server/server.conf', 'port 1194\n')
    put(bundle / 'etc-openvpn/server/ccd/dev_1', 'ifconfig-push 1\n')
    put(bundle / 'etc-strongswan/new.conf', 'new\n')
    put(bundle / 'etc-swanctl/conf.d/a.conf', 'conn\n')
    put(bundle / 'etc-ipsec.conf', 'new ipsec\n')
    put(bundle / 'etc-strongswan.d/x.conf', 'x\n')
    put(bundle / 'caddy-storage/certs/new.crt', 'new cert\n', 0o644)
    put(bundle / 'caddy-storage/locks/l', 'lock\n')


SCENARIOS = {
    'ok': ('', True), 'path3': ('install -m 600 *server.priv*', True),
    'start': ('systemctl start openvpn-server@server', True), 'minimal': ('', False),
}


def node(t, mode):
    """Узел, набор, страховочная копия (cp -a корня), WORK со списком выданных и заглушки."""
    fail, full = SCENARIOS[mode]
    live_node(t / 'root', full)
    bundle_tree(t / 'bundle', full)
    subprocess.run(['cp', '-a', str(t / 'root'), str(t / 'presnap')], check=True)
    put(t / 'work/issued-after-archive', '9\n 7 \nlast')
    (t / 'bin').mkdir()
    for name in ('systemctl', 'docker', 'chown', 'install', 'id'):
        put(t / 'bin' / name, STUB.replace('@INSTALL@', shutil.which('install')), 0o755)
    units = 'sing-box openvpn-server@server strongswan-starter anysda-ikev2-cert-sync.service'
    return {'FAIL_ON': fail, 'IDS': 'vpn2-control vpn2-panel' if full else '',
            'UNITS': units if full else '', 'ACTIVE': 'anysda-ikev2-cert-sync.path openvpn-server@server'}


def tree(base, t=None):
    """Дерево: путь, тип и права, содержимое или цель ссылки (каталог прогона t в ней - T)."""
    out = []
    for dirpath, dirs, files in os.walk(base):
        for name in dirs + files:
            p = Path(dirpath) / name
            body = os.readlink(p).replace(str(t), 'T') if p.is_symlink() else (p.read_bytes() if p.is_file() else b'')
            out.append((str(p.relative_to(base)), oct(p.lstat().st_mode), body))
    return sorted(out)


# Пути узла в вырезке - под $ROOT; подстановки (по порядку):
#   "${CADDY_STORAGE#/}" -> "${CADDY_STORAGE#$ROOT/}" (TOUCHED остаётся путём от корня узла);
#   "/${rel:?}", "/$(dirname -> с $ROOT (только rollback_path);  ' /var/lib/', '"/var/lib/', '=/var/lib/' -> с $ROOT;
#   ' /etc/', '"/etc/' -> с $ROOT;  ' /usr/local/' -> ' $ROOT/usr/local/' (anysda-svc.sh, vpn2-apply).
# В stderr bash текст $BASH_COMMAND несёт $ROOT/ - при сверке он снова /.
SUBS = (('"${CADDY_STORAGE#/}"', '"${CADDY_STORAGE#$ROOT/}"'), ('"/${rel:?}"', '"$ROOT/${rel:?}"'),
        ('"/$(dirname', '"$ROOT/$(dirname'),
        (' /var/lib/', ' $ROOT/var/lib/'), ('"/var/lib/', '"$ROOT/var/lib/'), ('=/var/lib/', '=$ROOT/var/lib/'),
        (' /etc/', ' $ROOT/etc/'), ('"/etc/', '"$ROOT/etc/'), (' /usr/local/', ' $ROOT/usr/local/'))


def bash_block():
    """Функции отката, остановка служб, применение и старт из скрипта. Удалить вместе с bash-телом."""
    text = RESTORE.read_text()
    marks = (('copy_into() {', '# ── Остановка сервисов'), ('# ── Остановка сервисов', '# ── Отзывы после архива'),
             ('# ── Применение ──', '# ── Pere-sync AGH'))
    body = ''.join(text[text.index(a):text.index(b)] for a, b in marks)
    for old, new in SUBS:
        body = body.replace(old, new)
    return body


BASH_RUN = '''set -euo pipefail
ROOT=$T/root PRESNAP_ROOT=$T/presnap PRESNAP_FINAL=$T/presnap.tar.gz.age WORK=$T/work
BUNDLE_DIR=$T/bundle LIVE_DB_MODE=600 LIVE_DB_OWNER=0:0
WG_BUNDLE="$BUNDLE_DIR/etc-wireguard" CADDY_STORAGE=$ROOT/var/lib/caddy/.local/share/caddy
STEP="остановка служб" TOUCHED=() DB_SWAPPED=0 CONTROL_STOPPED=0
{block}
echo DONE
'''

PY_RUN = '''import os, sys
from vpn2.backup.apply import apply_bundle, mark_good, start_services, stop_services
from vpn2.backup.errors import RestoreError
from vpn2.backup.rollback import Rollback
T = os.environ['T']
rb = Rollback(presnap_root=T + '/presnap', presnap_final=T + '/presnap.tar.gz.age', work=T + '/work',
              bundle_dir=T + '/bundle', root=T + '/root')
try:
    with rb:
        stop_services(rb)
        apply_bundle(rb, T + '/bundle')
        start_services(rb)
    mark_good(rb)
except RestoreError as e:
    sys.exit(e.code)
print('DONE')
'''
# chown владельца живого каталога в copy_into: bash зовёт программу chown, Rollback - os.chown.
_COPY_INTO_CHOWN = re.compile(r'^chown \d+:\d+ .*\n', re.M)


def scenario(t, mode, argv):
    """Прогон argv на узле в t: код, stdout, stderr, журнал заглушек, деревья корня и WORK."""
    env = dict(os.environ, T=str(t), PATH=f'{t}/bin:{os.environ["PATH"]}', PYTHONPATH=str(SRC), **node(t, mode))
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
    calls = (t / 'calls.log').read_text() if (t / 'calls.log').exists() else ''
    norm = (lambda s: s.replace(str(t), 'T').replace('$ROOT/', '/'))
    return (done.returncode, norm(done.stdout), norm(done.stderr), _COPY_INTO_CHOWN.sub('', norm(calls)),
            tree(t / 'root', t), tree(t / 'work', t))


@needs_tools
@pytest.mark.parametrize('mode', list(SCENARIOS))
def test_apply_matches_bash(tmp_path, mode):
    """Прямая сверка с bash: успех, сбой третьего пути, сбой старта службы, набор без компонентов.

    Удалить вместе с bash-телом.
    """
    old = scenario(tmp_path / 'bash', mode, ['bash', '-c', BASH_RUN.format(block=bash_block())])
    new = scenario(tmp_path / 'py', mode, [sys.executable, '-c', PY_RUN])
    assert new[0] == old[0] == (10 if mode in ('path3', 'start') else 0), old[2]
    for i in range(1, 6):
        assert new[i] == old[i], i
    assert EXPECT[mode] in old[2] and old[1].endswith('DONE\n') == (old[0] == 0)


# Получено прогоном bash-вырезки (scenario, bash) - строка stderr каждого сценария.
EXPECT = {
    'ok': 'anysda-restore: WARN: vpn2-apply mark openvpn не прошёл: last good не совпадает с server.conf\n',
    'path3': 'anysda-restore: FAILED на шаге «восстановление WireGuard»: install -m 600 "$WG_BUNDLE/$key"'
             ' "/etc/wireguard/keys/$key" (код 1) — возвращаю прежнее состояние\n',
    'start': 'anysda-restore: FAILED на шаге «запуск службы openvpn-server@server»: systemctl start "$1"'
             ' (код 1) — возвращаю прежнее состояние\n',
    'minimal': '',
}


class Fake:
    """run: systemctl, docker, chown, id - в журнал с кодом из fail; install без -o/-g; прочее - настоящее."""

    def __init__(self, fail=(), missing=(), ids=()):
        self.calls, self.fail, self.missing, self.ids = [], set(fail), set(missing), set(ids)

    def __call__(self, argv, **kw):
        line = ' '.join(argv)
        if argv[0] in self.missing:
            raise FileNotFoundError(argv[0])
        if argv[0] in ('systemctl', 'docker', 'chown', 'id'):
            self.calls.append(line)
            rc = 1 if line in self.fail or (argv[0] == 'id' and argv[1] not in self.ids) else 0
            return subprocess.CompletedProcess(argv, rc)
        if argv[0] == 'install':
            argv = [a for i, a in enumerate(argv) if a not in ('-o', '-g') and argv[i - 1] not in ('-o', '-g')]
        return subprocess.run(argv, **kw)


def make(t, run):
    for d in ('root', 'bundle', 'work', 'presnap'):
        (t / d).mkdir(exist_ok=True)
    return Rollback(presnap_root=t / 'presnap', presnap_final=t / 'snap.age', work=t / 'work',
                    bundle_dir=t / 'bundle', root=t / 'root', run=run)


def test_docker_missing_is_step_failure(tmp_path):
    """Нет программы docker: inspect - не 0, `docker info` - StepFailed 127 с текстом команды скрипта."""
    run = Fake(missing={'docker'})
    with pytest.raises(StepFailed) as caught:
        apply.stop_services(make(tmp_path, run), run=run, out=lambda _: None)
    assert (caught.value.rc, caught.value.cause) == (127, 'docker info > /dev/null')
    assert run.calls[-1] == 'systemctl stop strongswan-starter'


def test_issued_ids_like_read_r(tmp_path):
    """`while read -r`: края строк без пробелов, пробел внутри и не-ASCII целы, хвост без \\n пропущен."""
    ccd = tmp_path / 'root/etc/openvpn/server/ccd'
    names = ['5', 'a b', 'ключ', 'tail', 'keep']
    for name in names:
        put(ccd / f'dev_{name}', 'x\n')
    put(tmp_path / 'bundle/etc-openvpn/server/server.conf', 'port 1194\n')
    put(tmp_path / 'work/issued-after-archive', ' 5\t\na b\nключ\n\ntail')
    run = Fake()
    rb = make(tmp_path, run)
    apply.apply_bundle(rb, tmp_path / 'bundle', run=run, out=lambda _: None)
    assert sorted(p.name for p in ccd.iterdir()) == ['dev_keep', 'dev_tail']
    assert rb.touched == ['etc/openvpn'] and rb.step == 'восстановление состояния деплоя'


def test_repeat_apply_same_tree(tmp_path):
    """Повтор применения того же набора: дерево узла то же, учёт отката пополняется теми же путями."""
    live_node(tmp_path / 'root', False)
    bundle_tree(tmp_path / 'bundle', False)
    run, said = Fake(), []
    rb = make(tmp_path, run)
    apply.apply_bundle(rb, tmp_path / 'bundle', run=run, out=said.append)
    first = tree(tmp_path / 'root')
    apply.apply_bundle(rb, tmp_path / 'bundle', run=run, out=said.append)
    assert tree(tmp_path / 'root') == first and rb.touched == ['etc/anysda'] * 2
    assert not (tmp_path / 'root/etc/anysda/sso-runtime.json').exists()
    assert said == ['anysda-restore: Caddy storage отсутствует в архиве — оставляю текущее'] * 2


@pytest.mark.parametrize('mode', [0o755, 0o644, None])
def test_mark_good_only_executable_and_active(tmp_path, mode, capsys):
    """vpn2-apply без права исполнения или без файла - не зовётся; есть - зовётся, отказ - WARN."""
    tool = tmp_path / 'root/usr/local/sbin/vpn2-apply'
    if mode is not None:
        put(tool, '#!/bin/sh\nexit 0\n', mode)
    run = Fake()
    apply.mark_good(make(tmp_path, run), run=run)
    called = run.calls == ['systemctl is-active --quiet openvpn-server@server']
    assert called == (mode == 0o755) and capsys.readouterr().err == ''
    if mode == 0o755:
        put(tool, '#!/bin/sh\nexit 2\n', mode)
        apply.mark_good(make(tmp_path, run), run=run)
        assert 'WARN: vpn2-apply mark openvpn не прошёл' in capsys.readouterr().err


def test_deploy_state_from_bundle(tmp_path):
    """Состояние деплоя из набора ложится поверх живого каталога и создаёт его, если каталога нет."""
    state = tmp_path / 'root/var/lib/anysda-vpn2/state'
    put(tmp_path / 'bundle/deploy-state/mgmt-ips.txt', 'из архива\n', 0o600)
    for live in (False, True):
        if live:
            put(state / 'mgmt-ips.txt', 'текущее\n')
            put(state / 'other', 'остаётся\n')
        run = Fake()
        rb = make(tmp_path, run)
        apply.apply_bundle(rb, tmp_path / 'bundle', run=run, out=lambda _: None)
        assert (state / 'mgmt-ips.txt').read_text() == 'из архива\n'
        assert rb.touched == ['var/lib/anysda-vpn2/state']
    assert (state / 'other').read_text() == 'остаётся\n'


def test_bundle_without_deploy_state_keeps_live(tmp_path):
    """Архив старого выпуска: каталога deploy-state нет - текущее состояние деплоя не тронуто."""
    state = tmp_path / 'root/var/lib/anysda-vpn2/state'
    put(state / 'mgmt-ips.txt', 'текущее\n')
    run = Fake()
    rb = make(tmp_path, run)
    apply.apply_bundle(rb, tmp_path / 'bundle', run=run, out=lambda _: None)
    assert (state / 'mgmt-ips.txt').read_text() == 'текущее\n' and rb.touched == []


def test_start_without_svc_lib_and_sync_units(tmp_path):
    """Без anysda-svc.sh - `systemctl restart wg-quick@wg0`; остановленные sync-юниты - одним start."""
    put(tmp_path / 'root/etc/wireguard/wg0.conf', 'wg\n')
    put(tmp_path / 'root/etc/anysda/ikev2-domain', '')
    (tmp_path / 'bundle/caddy-storage').mkdir(parents=True)
    run = Fake(ids={'vpn2-control'})
    rb = make(tmp_path, run)
    rb.sync_units = ['anysda-ikev2-cert-sync.path']
    apply.start_services(rb, run=run, out=lambda _: None)
    assert [c for c in run.calls if not c.startswith('systemctl cat')] == [
        'systemctl start sing-box', 'systemctl restart wg-quick@wg0',
        'systemctl start openvpn-server@server', 'systemctl start anysda-failover-watchdog',
        'systemctl start strongswan-starter', 'systemctl start anysda-ikev2-routing',
        'systemctl start caddy', 'systemctl start anysda-ikev2-cert-sync.path', 'id vpn2-panel',
        'id vpn2-control', 'systemctl start vpn2-control.socket vpn2-control.service',
        'docker start anysda-vpn2 anysda-tgbot']
