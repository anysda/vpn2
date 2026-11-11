"""Транзакция панели: порядок замены, миграция под замком, откат по digest с правилом схемы."""

import io
import subprocess

import pytest

from vpn2 import fsutil
from vpn2.containers import compose, engine, transaction

OLD, NEW = 'sha256:old', 'sha256:new'
SERVICE = {'container_name': compose.PANEL, 'image': NEW, 'environment': {'A': 'p$w'},
           'labels': {compose.SPEC_LABEL: 'spec'}}


class Docker:
    """Память вместо docker: контейнеры, образы и файл служб."""

    def __init__(self, events, containers=None, images=(OLD, NEW), migration=0):
        self.events, self.containers = events, dict(containers or {})
        self.images, self.migration, self.file = set(images), migration, {}
        self.up_fails = False

    def inspect(self, name):
        return self.containers.get(name)

    def image_id(self, reference):
        return reference if reference in self.images else ''

    def call(self, *args, **_kwargs):
        self.events.append(('docker', *args))
        if args[:2] == ('rm', '-f'):
            self.containers.pop(args[2], None)
        if args[0] == 'run' and self.migration:
            raise engine.EngineError('docker run: код 1')
        return subprocess.CompletedProcess(args, 0, '', '')

    def services(self):
        return dict(self.file)

    def publish(self, name, service):
        self.events.append(('publish', service['image']))
        self.file[name] = service
        return True

    def up(self, name):
        self.events.append(('up', name))
        if self.up_fails:
            raise engine.EngineError('compose up: код 1')
        self.containers[name] = {'Image': self.file[name]['image'], 'Config': {'Env': []}}
        return True


def panel(image=OLD, env=()):
    return {'Image': image, 'Config': {'Env': list(env)}}


@pytest.fixture
def world(tmp_path):
    events, refused = [], set()

    def tool(args):
        action = next(arg for arg in args if arg in ('backup', 'image-contract', 'compatible', 'phase',
                                                     'record-rollback'))
        events.append(('tool', action, *[arg for arg in args if arg in ('--migrating', OLD, NEW)]))
        if action == 'image-contract':
            contract = args[args.index('--contract') + 1]
            fsutil.write_json(contract, {'image': args[args.index('--image') + 1]})
        if action == 'compatible' and 'previous-schema.json' in args[args.index('--contract') + 1]:
            return int('previous' in refused)
        return int(action in refused)

    def make(docker, health=True):
        out, err = io.StringIO(), io.StringIO()
        tx = transaction.Transaction(docker, tmp_path / 'state', tool=tool, health=lambda: health,
                                     out=out, err=err, installed=tmp_path / 'installed.json')
        return tx, out, err

    return events, refused, make


def test_replace_keeps_old_image_and_migrates_between_backups(world, tmp_path):
    events, _, make = world
    docker = Docker(events, {compose.PANEL: panel(env=['NUXT_NODE_HOSTS=ru=1']),
                             transaction.LEGACY_PREVIOUS: panel('sha256:ancient')})
    tx, out, _ = make(docker)
    tx.begin(NEW)
    assert tx.replace(NEW) == ['NUXT_NODE_HOSTS=ru=1']
    kinds = [event[:2] for event in events]
    assert kinds == [('tool', 'image-contract'), ('tool', 'compatible'), ('tool', 'backup'),
                     ('tool', 'image-contract'), ('docker', 'rm'), ('docker', 'rm'), ('tool', 'backup'),
                     ('docker', 'run'), ('tool', 'compatible')]
    assert events[1] == ('tool', 'compatible', '--migrating') and events[3][-1] == OLD
    assert fsutil.read_json(tmp_path / 'state/previous-image.json') == {'id': OLD}
    assert docker.containers == {}
    assert 'administrative changes paused for migration' in out.getvalue()


def test_migration_runs_under_exclusive_lock_of_data_dir(world, tmp_path):
    events, _, make = world
    tx, _, _ = make(Docker(events))
    tx.migrate(NEW)
    argv = list(events[-1][1:])
    assert argv[:4] == ['run', '--rm', '--network', 'none']
    assert f'{transaction.DATA}:{transaction.DATA}' in argv
    image = argv.index(NEW)
    assert argv[image - 2:image] == ['--entrypoint', 'flock']
    assert argv[image + 1:] == ['-n', f'{transaction.DATA}/.migration.lock', 'node', 'scripts/migrate.mjs']
    # Та же команда замка на хосте: свободный замок пускает, занятый - отказ без ожидания.
    lock = tmp_path / '.migration.lock'
    command = ['flock', '-n', str(lock), 'true']
    assert subprocess.run(command, check=False).returncode == 0
    with fsutil.lock(lock):
        assert subprocess.run(command, check=False).returncode == 1


def test_failed_migration_returns_previous_image_by_digest(world, tmp_path):
    events, _, make = world
    docker = Docker(events, {compose.PANEL: panel()}, migration=1)
    docker.file[compose.PANEL] = {**SERVICE, 'image': OLD}
    tx, out, err = make(docker)
    tx.begin(NEW)
    with pytest.raises(engine.EngineError):
        tx.replace(NEW)
    tx.failure()
    assert ('publish', OLD) in events and events.index(('publish', OLD)) < events.index(('up', compose.PANEL))
    assert docker.containers[compose.PANEL]['Image'] == OLD
    assert docker.file[compose.PANEL]['environment'] == {'A': 'p$w'}
    assert transaction.VERIFIED in out.getvalue() and err.getvalue() == ''
    assert not (tmp_path / 'state/previous-image.json').exists()
    assert events[-1][:2] == ('tool', 'record-rollback')


def test_rollback_from_docker_run_install_uses_candidate_service(world):
    """Прежняя панель запущена docker run: службы в файле ещё нет, берётся описание кандидата."""
    events, _, make = world
    docker = Docker(events, {compose.PANEL: panel()}, migration=1)
    tx, out, _ = make(docker)
    tx.candidate = dict(SERVICE)
    tx.begin(NEW)
    with pytest.raises(engine.EngineError):
        tx.replace(NEW)
    tx.failure()
    assert docker.file[compose.PANEL]['image'] == OLD and transaction.VERIFIED in out.getvalue()


def test_rollback_refused_on_incompatible_schema(world):
    events, refused, make = world
    docker = Docker(events, {compose.PANEL: panel()})
    docker.file[compose.PANEL] = dict(SERVICE)
    tx, _, err = make(docker)
    tx.begin(NEW)
    tx.replace(NEW)
    docker.containers[compose.PANEL] = panel(NEW)
    refused.add('previous')
    assert tx.rollback() is False
    assert err.getvalue().strip() == transaction.INCOMPATIBLE
    assert not [event for event in events if event[0] in ('publish', 'up')]
    assert docker.containers[compose.PANEL]['Image'] == NEW
    # Отрицательный контроль: та же установка с совместимой схемой откатывается.
    refused.clear()
    assert tx.rollback() is True
    assert docker.containers[compose.PANEL]['Image'] == OLD


@pytest.mark.parametrize('case', ['never-replaced', 'image-pruned'])
def test_rollback_refused_without_previous_release(world, case):
    events, _, make = world
    docker = Docker(events, {compose.PANEL: panel()})
    docker.file[compose.PANEL] = dict(SERVICE)
    tx, _, err = make(docker)
    if case == 'image-pruned':
        tx.begin(NEW)
        tx.replace(NEW)
        docker.images.discard(OLD)
    assert tx.rollback() is False
    assert err.getvalue().strip() == transaction.NO_PREVIOUS
    assert ('up', compose.PANEL) not in events


def test_rollback_with_dead_panel_is_reported(world):
    events, _, make = world
    docker = Docker(events, {compose.PANEL: panel()})
    docker.file[compose.PANEL] = dict(SERVICE)
    tx, _, err = make(docker, health=False)
    tx.begin(NEW)
    tx.replace(NEW)
    tx.failure()
    assert err.getvalue().splitlines() == [transaction.UNHEALTHY, transaction.NO_AUTO]


def test_first_install_failure_has_nothing_to_return(world):
    events, _, make = world
    tx, _, err = make(Docker(events, migration=1))
    tx.begin(NEW)
    with pytest.raises(engine.EngineError):
        tx.replace(NEW)
    tx.failure()
    assert err.getvalue().strip() == transaction.FIRST_FAILED
    assert not [event for event in events if event[0] in ('publish', 'up')]


def test_second_deploy_is_refused_while_first_holds_lock(world):
    events, _, make = world
    first, _, _ = make(Docker(events))
    first.begin(NEW)
    second, _, _ = make(Docker(events))
    with pytest.raises(transaction.TransactionError, match=transaction.BUSY):
        second.begin(NEW)
    first._lock.close()
    second.begin(NEW)


def test_incompatible_candidate_never_touches_running_panel(world):
    events, refused, make = world
    refused.add('compatible')
    docker = Docker(events, {compose.PANEL: panel()})
    tx, _, _ = make(docker)
    with pytest.raises(transaction.TransactionError):
        tx.begin(NEW)
    tx.failure()
    assert docker.containers[compose.PANEL]['Image'] == OLD
    assert not [event for event in events if event[0] == 'docker']


def test_failed_backup_leaves_running_panel_and_database(world):
    """Копия обязательна: без неё прежнюю панель не останавливают и базу не мигрируют."""
    events, refused, make = world
    refused.add('backup')
    docker = Docker(events, {compose.PANEL: panel()})
    tx, _, _ = make(docker)
    tx.begin(NEW)
    with pytest.raises(transaction.TransactionError):
        tx.replace(NEW)
    assert docker.containers[compose.PANEL]['Image'] == OLD
    assert not [event for event in events if event[0] == 'docker']
    # Отрицательный контроль: с копией тот же вызов останавливает панель и мигрирует.
    refused.clear()
    tx.replace(NEW)
    assert ('docker', 'rm', '-f', compose.PANEL) in events


def test_host_files_return_on_failure(world, tmp_path):
    events, _, make = world
    kept, created = tmp_path / 'Caddyfile', tmp_path / 'api-managed'
    kept.write_text('old')
    kept.chmod(0o640)
    restored = []
    tx, out, _ = make(Docker(events))
    tx.files_restored = lambda: restored.append(kept.read_text())
    tx.begin(NEW)
    tx.guard_files([kept, created])
    kept.write_text('new')
    created.write_text('')
    tx.failure()
    assert kept.read_text() == 'old' and kept.stat().st_mode & 0o777 == 0o640
    assert not created.exists() and restored == ['old']
    assert 'panel host config restored' in out.getvalue()
    assert not (tmp_path / 'state/files.pending').exists()


def test_finish_closes_transaction(world, tmp_path):
    events, _, make = world
    tx, _, err = make(Docker(events))
    tx.begin(NEW)
    tx.guard_files([tmp_path / 'absent'])
    tx.finish()
    assert events[-1][:2] == ('tool', 'phase')
    tx.failure()
    assert err.getvalue() == '' and not (tmp_path / 'state/files.pending').exists()
