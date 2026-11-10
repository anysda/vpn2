"""Откат restore: учёт заменённого и возврат из страховочной копии (copy_into/rollback_path/rollback).

Повторяет блок anysda-restore.sh между `copy_into() {` и остановкой служб: те же шаги, порядок,
тексты stderr и код 10. Внешние программы - через `run=`, корень узла - параметром `root`.
"""
import os
import shutil
import signal
import subprocess
import sys

from vpn2.backup.errors import RestoreError

ROLLBACK_UNITS = ('sing-box', 'openvpn-server@server', 'anysda-failover-watchdog',
                  'strongswan-starter', 'anysda-ikev2-routing')
CONTAINERS = ('anysda-vpn2', 'anysda-tgbot')
DB_DIR = 'var/lib/anysda-vpn2'
CONTROL_DIR = 'var/lib/vpn2-control'
_QUIET = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}


class StepFailed(Exception):
    """Команда шага вернула не 0: rc и cause - как `$?` и `$BASH_COMMAND` в ловушке ERR."""

    def __init__(self, rc, cause):
        super().__init__(cause)
        self.rc = rc
        self.cause = cause


class _Signalled(BaseException):
    """Сигнал INT или TERM внутри блока: код и причина как у trap в скрипте."""

    def __init__(self, rc, cause):
        super().__init__(cause)
        self.rc = rc
        self.cause = cause


_SIGNALS = {signal.SIGINT: (130, 'получен сигнал INT'), signal.SIGTERM: (143, 'получен сигнал TERM')}


def _raise_signalled(signum, _frame):
    raise _Signalled(*_SIGNALS[signum])


_pending = []


def _hold(signum, _frame):
    """Сигнал посреди отката не прерывает его (обработчик-функция, дети его не наследуют)."""
    _pending.append(signum)


class Rollback:
    """Транзакция restore после остановки служб: исключение или INT/TERM в блоке - откат и код 10.

    touched - TOUCHED (пути от корня или `db`), db_swapped/control_stopped - DB_SWAPPED/CONTROL_STOPPED,
    sync_units - CADDY_SYNC_UNITS, step - STEP. Обработчики INT/TERM ставятся на время блока.
    """

    def __init__(self, *, presnap_root, presnap_final, work, bundle_dir, live_db_mode='600',
                 live_db_owner='0:0', root='/', run=subprocess.run, err=None):
        self.presnap_root, self.presnap_final = str(presnap_root), str(presnap_final)
        self.work, self.bundle_dir, self.root = str(work), str(bundle_dir), str(root)
        self.live_db_mode, self.live_db_owner = live_db_mode, live_db_owner
        self.run, self.err = run, err
        self.step = 'остановка служб'
        self.touched, self.sync_units = [], []
        self.db_swapped = self.control_stopped = False
        self._saved = None
        self._done = False

    def live(self, rel):
        """Путь узла `/rel` под корнем root."""
        return os.path.join(self.root, rel)

    def touch(self, *items):
        """`TOUCHED+=(items)`: путь от корня без `/` в начале или `db`."""
        self.touched.extend(items)

    def stopped(self, unit):
        """Служба остановлена шагом restore: vpn2-control - CONTROL_STOPPED=1, иная - в CADDY_SYNC_UNITS."""
        if unit.startswith('vpn2-control'):
            self.control_stopped = True
        else:
            self.sync_units.append(unit)

    def _ok(self, argv, **kw):
        try:
            return self.run(argv, check=False, **kw).returncode == 0
        except OSError:
            return False

    def copy_into(self, src, dst):
        """`cp -a src/. dst/`, у живого dst прежние владелец и режим; сбой cp - StepFailed."""
        src, dst = str(src), str(dst)
        owner = None
        if os.path.isdir(dst):
            st = os.stat(dst)
            owner = (st.st_uid, st.st_gid, st.st_mode & 0o7777)
        else:
            os.makedirs(dst, exist_ok=True)
        try:
            rc = self.run(['cp', '-a', src + '/.', dst + '/'], check=False).returncode
        except OSError:
            rc = 127
        if rc:
            raise StepFailed(rc, 'cp -a "$src/." "$dst/"')
        if owner:
            os.chown(dst, owner[0], owner[1])
            os.chmod(dst, owner[2])

    def rollback_path(self, rel):
        """`/rel` заново из страховочной копии; не было там - только удалить. True - вернулось."""
        if not rel:
            raise ValueError('rel: parameter null or not set')
        if not self._ok(['rm', '-rf', '--', self.live(rel)]):
            return False
        saved = os.path.join(self.presnap_root, rel)
        if not (os.path.lexists(saved)):
            return True
        parent = self.live(os.path.dirname(rel))
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            return False
        return self._ok(['cp', '-a', saved, parent + '/'])

    def start_unit(self, unit):
        """Запуск существующей службы; отказ - StepFailed, как ERR на `systemctl start "$1"`."""
        self.step = f'запуск службы {unit}'
        if not self._ok(['systemctl', 'cat', unit], **_QUIET):
            return
        try:
            rc = self.run(['systemctl', 'start', unit], check=False).returncode
        except OSError:
            rc = 127
        if rc:
            raise StepFailed(rc, 'systemctl start "$1"')

    def __enter__(self):
        if self._saved is not None:
            raise RuntimeError('Rollback: блок уже открыт')
        self._done = False
        self._saved = {s: signal.getsignal(s) for s in _SIGNALS}
        for s in _SIGNALS:
            signal.signal(s, _raise_signalled)
        return self

    def __exit__(self, _exc_type, exc, _tb):
        for s in _SIGNALS:
            signal.signal(s, _hold)
        saved, self._saved = self._saved, None
        late, rolled = [], True
        try:
            if exc is None or not isinstance(exc, (Exception, _Signalled)):
                late, rolled = list(_pending), False
                return False
            rc, cause = (exc.rc, exc.cause) if isinstance(exc, (StepFailed, _Signalled)) else (1, str(exc))
            self.rollback(rc, cause)
            late, rolled = [], True
        finally:
            _pending.clear()
            for s, handler in saved.items():
                signal.signal(s, handler)
            if not rolled:
                for s in late:
                    signal.raise_signal(s)
        raise RestoreError(10, f'откат после сбоя на шаге «{self.step}»') from exc

    def _say(self, text):
        print(f'anysda-restore: {text}', file=self.err or sys.stderr, flush=True)

    def _rm_f(self, *rels):
        return self._ok(['rm', '-f', *(self.live(r) for r in rels)])

    def _db_back(self):
        """Ветка `db` отката; True - как `fi && back+=` (итог последней команды ветки)."""
        db = f'{DB_DIR}/db.sqlite'
        if not self.db_swapped:
            if os.path.isdir(os.path.join(self.work, 'db-before')):
                try:
                    self.copy_into(os.path.join(self.work, 'db-before'), self.live(DB_DIR))
                except (StepFailed, OSError):
                    pass
            return self._rm_f(f'{DB_DIR}/.db-restore.sqlite')
        saved = os.path.join(self.presnap_root, db)
        if not os.path.isfile(saved):
            return self._rm_f(db)
        uid, _, gid = self.live_db_owner.partition(':')
        tmp = self.live(f'{DB_DIR}/.db-rollback.sqlite')
        return (self._ok(['install', '-m', self.live_db_mode, '-o', uid, '-g', gid, saved, tmp])
                and self._rm_f(f'{db}-wal', f'{db}-shm', f'{db}-journal')
                and self._ok(['mv', tmp, self.live(db)]))

    def rollback(self, rc, cause):
        """Тело `rollback rc cause` без `exit 10`: вернуть заменённое, запустить службы, отчёт в stderr.

        Каждый шаг идёт, даже если прежний не удался (как `set +e`); повторный вызов - ничего.
        """
        if self._done:
            return
        self._done = True
        back, failed, down, seen = [], [], [], set()
        self._say(f'FAILED на шаге «{self.step}»: {cause} (код {rc}) — возвращаю прежнее состояние')
        for item in self.touched:
            if item in seen:
                continue
            seen.add(item)
            if item == 'db':
                (back if self._safe(self._db_back) else failed).append(f'/{DB_DIR}/db.sqlite')
                before = os.path.join(self.work, 'control-before-restore')
                if os.path.isdir(before):
                    ok = self._ok(['rm', '-rf', self.live(CONTROL_DIR)]) \
                        and self._ok(['mv', before, self.live(CONTROL_DIR)])
                    (back if ok else failed).append(f'/{CONTROL_DIR}')
            else:
                (back if self._safe(self.rollback_path, item) else failed).append(f'/{item}')
        for unit in ROLLBACK_UNITS:
            if self._ok(['systemctl', 'cat', unit], **_QUIET) \
                    and not self._ok(['systemctl', 'start', unit], **_QUIET):
                down.append(unit)
        if os.path.isfile(self.live('etc/wireguard/wg0.conf')) \
                and not self._ok(['systemctl', 'restart', 'wg-quick@wg0'], **_QUIET):
            down.append('wg-quick@wg0')
        if shutil.which('swanctl'):
            self._ok(['swanctl', '--load-all'], **_QUIET)
        if os.path.isdir(os.path.join(self.bundle_dir, 'caddy-storage')):
            for unit in ('caddy', *self.sync_units):
                if not self._ok(['systemctl', 'start', unit], **_QUIET):
                    down.append(unit)
        if self.control_stopped and not self._ok(
                ['systemctl', 'start', 'vpn2-control.socket', 'vpn2-control.service'], **_QUIET):
            down.append('vpn2-control')
        for name in CONTAINERS:
            if self._ok(['docker', 'container', 'inspect', name], **_QUIET) \
                    and not self._ok(['docker', 'start', name], **_QUIET):
                down.append(name)
        self._say(f'возвращено из страховочной копии: {" ".join(back) or "ничего не заменялось"}')
        if failed:
            self._say(f'НЕ возвращено: {" ".join(failed)} — бери из {self.presnap_final}')
        if down:
            self._say(f'не запустились: {" ".join(down)}')
        else:
            self._say('службы и контейнеры прежнего состояния запущены')
        self._say(f'страховочный снимок: {self.presnap_final}')

    @staticmethod
    def _safe(step, *args):
        """Шаг отката, упавший исключением, - как команда с кодом не 0: False, откат идёт дальше."""
        try:
            return step(*args)
        except Exception:  # noqa: BLE001 - в bash под `set +e` любой сбой шага только отметка
            return False
