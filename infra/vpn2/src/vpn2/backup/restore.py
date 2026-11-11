"""Восстановление узла из архива: ход прежнего anysda-restore.sh от разбора аргументов до итога.

Коды выхода: 2 настройки и аргументы, 3 нет архива, 4 расшифровка, 5 архив или узел не годятся,
6 живая база без --force, 7 99-verify, 8 пароли не пересинхронизированы, 9 несовместимая версия,
10 откат после сбоя посреди замены.

`--preflight` - проход деплоя перед открытием выпуска: все отказы до первого изменения узла теми же
кодом и строкой (архив, расшифровка, манифест, версия, живая база без --force), службы и данные не тронуты.
"""
import glob
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vpn2.backup import apply, archive, check, env, prepare, revoke, select, stages, verify
from vpn2.backup.archive import ArchiveError
from vpn2.backup.errors import RestoreError
from vpn2.backup.rollback import Rollback, StepFailed

REVOKE_CAUSE = 'python3 - "$BUNDLE_DIR" "$WORK/issued-after-archive"'
NO_PASSPHRASE = ('anysda-restore FATAL: ANYSDA_BACKUP_PASSPHRASE пуст в {env_file}.\n'
                 '  Заполни backup.passphrase в config.yaml и прогони ./deploy.sh 26-backup ru')


@dataclass(frozen=True)
class Node:
    """Пути узла; root переносит всё дерево под каталог (проверки без прав root)."""

    root: str = '/'
    env_file: str = env.DEFAULT_ENV_FILE
    source_root_file: str = '/etc/anysda/source-root'
    default_source_root: str = verify.DEFAULT_SOURCE_ROOT
    state_dir: str = 'var/lib/anysda-vpn2'
    template_dir: str = 'tmp/anysda'

    def at(self, rel):
        """Путь узла `/rel` под корнем."""
        return os.path.join(self.root, rel)

    def source_root(self):
        """Клон, из которого шёл деплой (отметку пишет стадия 26-backup); без отметки - /opt/anysda-vpn2."""
        try:
            text = Path(self.source_root_file).read_text(errors='surrogateescape').rstrip('\n')
        except OSError:
            text = ''
        return text or self.default_source_root


def _settings(node):
    try:
        return env.settings(env.read_env(node.env_file), node.env_file, lazy_s3=True)
    except env.BackupEnvError as exc:
        text = f'anysda-restore: {node.env_file} не доступен' if exc.code == 2 else str(exc)
        raise RestoreError(exc.code, text) from None


def _pick(args, settings, run, err):
    """Имя архива и откуда он: latest - самый свежий, иначе имя из аргумента (каталог отбрасывается)."""
    if args.name == 'latest':
        return select.resolve_latest(settings, run=run, err=err)
    name = os.path.basename(args.name)
    if os.path.isfile(os.path.join(settings.local_dir, name)):
        return name, 'local'
    if settings.backend != 's3':
        raise RestoreError(3, f'anysda-restore: архив {name} не найден в {settings.local_dir}')
    return name, 'S3'


def _open(archive_path, work, passphrase, run):
    """Расшифровать и распаковать архив в work; вернуть каталог набора anysda-vpn2-*."""
    plain = work / 'restore.tar.gz'
    try:
        archive.decrypt(archive_path, plain, passphrase)
        archive.unpack(plain, work, run=run)
    except ArchiveError as exc:
        raise RestoreError(exc.code, exc.message) from None
    archive.wipe(plain, run=run)
    found = sorted(path for path in glob.glob(os.path.join(glob.escape(str(work)), 'anysda-vpn2-*'))
                   if os.path.isdir(path) and not os.path.islink(path))
    if not found:
        raise RestoreError(5, 'anysda-restore: в архиве нет ожидаемой папки anysda-vpn2-*')
    return found[0]


def _db_stat(live_db):
    """Владелец и права живой БД для отката (умолчания скрипта - 0:0 и 600)."""
    if not os.path.isfile(live_db):
        return '0:0', '600'
    info = os.stat(live_db)
    return f'{info.st_uid}:{info.st_gid}', format(info.st_mode & 0o7777, 'o')


def _revoke(rb, bundle_dir, work, node, err):
    rb.step = 'перенос отзывов в архив'
    try:
        code = revoke.run(bundle_dir, os.path.join(work, 'issued-after-archive'),
                          live_db=node.at(f'{node.state_dir}/db.sqlite'),
                          live_pki=node.at('etc/openvpn/server/pki'), live_wg_dir=node.at('etc/wireguard'),
                          live_ike_conf=node.at('etc/swanctl/conf.d/anysda-clients.conf'),
                          live_ccd=node.at('etc/openvpn/server/ccd'), runner=rb.run)
    except revoke.RevokeError as exc:
        print(exc.message, file=err, flush=True)
        raise StepFailed(exc.code, REVOKE_CAUSE) from None
    if code:
        raise StepFailed(code, REVOKE_CAUSE)


def _check(bundle_dir):
    lines = _Lines()
    return check.check_bundle(bundle_dir, out=lines, err=lines), lines.text()


class _Lines:
    """Сборщик вывода сверки: stdout и stderr одной лентой, как `2>&1` в скрипте."""

    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)

    def flush(self):
        pass

    def text(self):
        return ''.join(self.parts).rstrip('\n')


def _replace(rb, bundle_dir, work, node, run, say, err):
    """Замена данных узла под откатом: сбой или INT/TERM внутри - прежнее состояние и код 10."""
    with rb:
        apply.stop_services(rb, run=run, out=say)
        _revoke(rb, bundle_dir, work, node, err)
        apply.apply_bundle(rb, bundle_dir, run=run, out=say)
        apply.start_services(rb, run=run, out=say)
    apply.mark_good(rb, run=run)


def restore(argv, *, node=Node(), run=subprocess.run, out=None, err=None, now=None, environ=None):
    """Восстановить узел из архива; ошибки шагов - RestoreError с кодом выхода и текстом для stderr."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err

    def say(line):
        print(line, file=out, flush=True)

    settings = _settings(node)
    args = select.parse_args(argv)
    if not settings.passphrase:
        raise RestoreError(2, NO_PASSPHRASE.format(env_file=node.env_file))
    name, source = _pick(args, settings, run, err)
    if args.dry_run:
        say(select.dry_run_text(name, source))
        return
    if source == 'S3' and not select.s3_pull(settings, name, settings.local_dir, run=run):
        raise RestoreError(3, f'anysda-restore: {name} не скачан из S3')
    archive_path = os.path.join(settings.local_dir, name)
    say(f'anysda-restore: source = {archive_path}')
    source_root = node.source_root()
    with archive.workdir(node.at(node.state_dir), '.restore-tmp.', run=run) as work:
        bundle_dir = _open(archive_path, work, settings.passphrase, run)
        manifest = verify.check_manifest(bundle_dir, archive_name=name, out=out, err=err)
        verify.check_compat(manifest, bundle_dir, work, source_root=source_root, run=run,
                            allow=args.allow_incompatible, out=out, err=err)
        prepare.check_bundle_report(bundle_dir, checker=_check, out=lambda line: print(line, file=err, flush=True))
        prepare.wg_key_material(bundle_dir, run=run)
        prepare.config_template(source_root, dest_dir=work / 'template' if args.preflight
                                else node.at(node.template_dir))
        live_db = node.at(f'{node.state_dir}/db.sqlite')
        prepare.guards(force=args.force, bundle_dir=bundle_dir, db_path=live_db, run=run)
        if args.preflight:
            return
        owner, mode = _db_stat(live_db)
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        stamp = moment.strftime('%Y-%m-%dT%H%M%S.') + f'{moment.microsecond * 1000:09d}Z'
        final = prepare.pre_restore_snapshot(settings, stamp, work=work, root=node.root, live_db=live_db,
                                             run=run, out=say)
        rb = Rollback(presnap_root=work / 'pre-restore', presnap_final=final, work=work, bundle_dir=bundle_dir,
                      live_db_mode=mode, live_db_owner=owner, root=node.root, run=run, err=err)
        _replace(rb, bundle_dir, work, node, run, say, err)
        env_file = stages.stage_env(source_root, work, environ=environ)
        fail = stages.resync(source_root, env_file, work, run=run, out=out, err=err)
        stages.smoke(source_root, env_file, final, run=run, out=out, err=err)
        stages.finish(fail, archive_path, final, out=out)


def main(argv, *, node=Node(), err=None, **kwargs):
    """Код выхода команды; текст RestoreError - в stderr (откат уже отчитался сам)."""
    err = sys.stderr if err is None else err
    old_umask = os.umask(0o077)
    try:
        with archive.exit_on_signals():
            restore(argv, node=node, err=err, **kwargs)
    except RestoreError as exc:
        if exc.code != 10:
            print(exc.message, file=err, flush=True)
        return exc.code
    finally:
        os.umask(old_umask)
    return 0
