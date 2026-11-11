"""Транзакция замены панели: замок, копия, миграция под замком, откат по digest.

Откат возвращает прежний образ, данные не трогает: база, отзывы, сроки и PKI остаются
текущими. Поэтому прежний образ запускается, только если понимает текущую схему базы.
"""

import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path

from vpn2 import fsutil
from vpn2.containers import compose, engine

STATE = '/var/lib/vpn2-deploy/panel'
TOOL = '/tmp/anysda/deploy_state.py'
INSTALLED = '/var/lib/vpn2-deploy/installed.json'
DATA = '/var/lib/anysda-vpn2'
LEGACY_PREVIOUS = 'anysda-vpn2-previous'
HEALTH = 'http://127.0.0.1:51821/api/version'
NO_PREVIOUS = 'rollback refused: no previous release; repair the current installation'
INCOMPATIBLE = 'rollback refused: incompatible schema; keep current DB/PKI and deploy a compatible image'
UNHEALTHY = 'rollback failed health check; inspect service status'
VERIFIED = 'panel rollback verified; current database, revocations, expirations, operations and PKI retained'
NO_AUTO = ('automatic rollback unavailable; deploy a schema-compatible release; '
           'do not restore an old access snapshot')
FIRST_FAILED = 'installation failed; no previous release exists'
BUSY = 'panel deploy already running'


class TransactionError(Exception):
    pass


def _tool(args):
    return subprocess.run([sys.executable, os.environ.get('VPN2_DEPLOY_STATE') or TOOL, *args],
                          check=False).returncode


def _health():
    return engine.wait(lambda: engine.http_ok(HEALTH), 30, 1)


class Transaction:
    def __init__(self, docker, state=STATE, *, tool=_tool, health=_health, out=sys.stdout, err=sys.stderr,
                 installed=INSTALLED):
        self.docker, self.state = docker, Path(state)
        self.tool, self.health, self.out, self.err = tool, health, out, err
        self.installed = Path(installed)
        self.changed = self.old = self.replaced = False
        self.files = []
        self.candidate = None
        self.files_restored = None
        self._lock = None

    def _must(self, *args):
        if self.tool([str(arg) for arg in args]):
            raise TransactionError(f'deploy_state {args[-1] if args[0] != "--state" else args[2]}: отказ')

    def lock(self):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = open(self.state / 'transaction.lock', 'a')  # noqa: SIM115 - замок живёт до конца процесса
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TransactionError(BUSY) from None

    def begin(self, image):
        """Замок и проверка образа. Работающую панель не трогает."""
        self.lock()
        self._must('image-contract', '--image', image, '--contract', self.state / 'candidate-schema.json')
        self._must('compatible', '--contract', self.state / 'candidate-schema.json', '--migrating')
        self.changed = True

    def guard_files(self, files):
        """Файлы хоста, принадлежащие выпуску панели: неудачный деплой возвращает их."""
        pending = self.state / 'files.pending'
        shutil.rmtree(pending, ignore_errors=True)
        pending.mkdir(mode=0o700)
        for file in files:
            self.files.append(str(file))
            if os.path.lexists(file):
                target = pending / str(file).lstrip('/')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, target, follow_symlinks=False)
                stat = os.lstat(file)
                os.chown(target, stat.st_uid, stat.st_gid, follow_symlinks=False)

    def restore_files(self):
        pending = self.state / 'files.pending'
        for file in self.files:
            saved = pending / file.lstrip('/')
            if os.path.lexists(saved):
                shutil.copy2(saved, file, follow_symlinks=False)
                stat = os.lstat(saved)
                os.chown(file, stat.st_uid, stat.st_gid, follow_symlinks=False)
            else:
                Path(file).unlink(missing_ok=True)
        if self.files:
            if self.files_restored:
                self.files_restored()
            print('panel host config restored', file=self.out)

    def replace(self, image):
        """Копия, остановка прежней панели и миграция базы под новую. Вернуть env прежней панели."""
        # Первая копия обязательна, пока прежняя панель и VPN ещё работают.
        self._must('--state', self.state, 'backup')
        previous_installed = self.state / 'previous-installed.json'
        if self.installed.is_file():
            shutil.copy(self.installed, previous_installed)
        else:
            previous_installed.unlink(missing_ok=True)
        shutil.rmtree(self.state / 'files', ignore_errors=True)
        if (self.state / 'files.pending').is_dir():
            shutil.copytree(self.state / 'files.pending', self.state / 'files', symlinks=True)
        self.replaced = True
        env = []
        old = self.docker.inspect(compose.PANEL)
        if old:
            env = old['Config'].get('Env') or []
            self._must('image-contract', '--image', old['Image'], '--contract',
                       self.state / 'previous-schema.json')
            fsutil.write_json(self.state / 'previous-image.json', {'id': old['Image']})
            self.old = True
            # Прежний контейнер снимается, образ остаётся: перезагрузка хоста посреди
            # миграции не поднимет старый код на новой схеме рядом с новой панелью.
            self.docker.call('rm', '-f', compose.PANEL)
            self.docker.call('rm', '-f', LEGACY_PREVIOUS, check=False)
        print('administrative changes paused for migration; VPN services remain running', file=self.out)
        # Вторая копия ловит изменения между первой и окном обслуживания.
        self._must('--state', self.state, 'backup')
        self.migrate(image)
        self._must('compatible', '--contract', self.state / 'candidate-schema.json')
        return env

    def migrate(self, image):
        """migrate.mjs образа под замком каталога базы: вторая миграция рядом не идёт."""
        self.docker.call('run', '--rm', '--network', 'none', '--user', '0:0',
                         '-v', f'{DATA}:{DATA}', '-e', f'NUXT_DATABASE_URL={compose.DB_URL}',
                         '--entrypoint', 'flock', image, '-n', f'{DATA}/.migration.lock',
                         'node', 'scripts/migrate.mjs', capture_output=False)

    def previous_image(self):
        saved = fsutil.read_json(self.state / 'previous-image.json', default=None)
        if isinstance(saved, dict) and saved.get('id'):
            return saved['id']
        legacy = self.docker.inspect(LEGACY_PREVIOUS)  # откат сразу после обновления с docker run
        return legacy['Image'] if legacy else ''

    def rollback(self):
        """Вернуть прежний образ панели по digest; данные остаются текущими."""
        contract = self.state / 'previous-schema.json'
        image = self.previous_image() if contract.is_file() else ''
        if not image or not self.docker.image_id(image):
            print(NO_PREVIOUS, file=self.err)
            return False
        if self.tool(['compatible', '--contract', str(contract)]):
            print(INCOMPATIBLE, file=self.err)
            return False
        service = self.docker.services().get(compose.PANEL) or self.candidate
        if not service:
            print(NO_PREVIOUS, file=self.err)
            return False
        service = {**service, 'image': image, 'labels': {compose.SPEC_LABEL: 'rollback'}}
        try:
            self.docker.publish(compose.PANEL, service)
            self.docker.up(compose.PANEL)
            self.docker.call('rm', '-f', LEGACY_PREVIOUS, check=False)
        except engine.EngineError as error:
            print(f'rollback failed: {error}', file=self.err)
            return False
        if not self.health():
            print(UNHEALTHY, file=self.err)
            return False
        if self.tool(['record-rollback']):
            return False
        (self.state / 'previous-image.json').unlink(missing_ok=True)
        print(VERIFIED, file=self.out)
        return True

    def failure(self):
        """Стадия упала после begin: вернуть файлы хоста и прежнюю панель."""
        if not self.changed:
            return
        self.changed = False
        try:
            self.restore_files()
        except OSError:
            print('panel host config restore failed', file=self.err)
        shutil.rmtree(self.state / 'files.pending', ignore_errors=True)
        if self.old:
            if not self.rollback():
                print(NO_AUTO, file=self.err)
        elif self.replaced:
            print(FIRST_FAILED, file=self.err)

    def finish(self):
        if not self.health():
            raise TransactionError('панель не отвечает на /api/version')
        self._must('--state', self.state, 'phase', '--phase', 'complete')
        self.changed = False
        shutil.rmtree(self.state / 'files.pending', ignore_errors=True)
