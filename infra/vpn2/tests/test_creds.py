"""Хранилище секретов entry (vpn2.creds) и область secrets config.yaml: запись, виды, перенос мест 1.2.0, стирание."""
import contextlib
import grp
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from vpn2 import config, creds, deploy

GROUP = grp.getgrgid(os.getgid()).gr_name
ROOT = Path(__file__).resolve().parents[3]


class Store(unittest.TestCase):
    """Хранилище - из creds_store (conftest); виды контейнеров - с группой этого пользователя."""
    def setUp(self):
        self.root = creds.STORE.parent
        for target, value in (('CLASH', self.root / 'clash-secret.txt'),
                              ('SHARED', {view: (GROUP, names) for view, (_, names) in creds.SHARED.items()})):
            patcher = mock.patch.object(creds, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class WriteReadTests(Store):
    def test_value_is_encrypted_at_rest_and_published_to_views(self):
        self.assertTrue(creds.write('telegram_bot_token', '123:token'))
        self.assertNotIn(b'123:token', (self.root / 'store/telegram_bot_token.cred').read_bytes())
        self.assertEqual(creds.read('telegram_bot_token'), '123:token')
        root, bot = self.root / 'run/root/telegram_bot_token', self.root / 'run/bot/telegram_bot_token'
        self.assertEqual((root.read_text(), root.stat().st_mode & 0o777), ('123:token', 0o600))
        self.assertEqual((bot.read_text(), bot.stat().st_mode & 0o777), ('123:token', 0o640))
        self.assertEqual((self.root / 'run/bot').stat().st_mode & 0o777, 0o750)
        self.assertFalse((self.root / 'run/bot/session_secret').exists(), 'вид бота - только его секреты')

    def test_same_value_is_not_rewritten_and_empty_removes(self):
        creds.write('s3_secret_key', 'k1')
        stamp = (self.root / 'store/s3_secret_key.cred').stat().st_ino
        self.assertFalse(creds.write('s3_secret_key', 'k1'))
        self.assertEqual((self.root / 'store/s3_secret_key.cred').stat().st_ino, stamp)
        self.assertTrue(creds.write('s3_secret_key', ''))
        self.assertEqual(creds.read('s3_secret_key'), '')
        self.assertFalse((self.root / 'run/root/s3_secret_key').exists())

    def test_reboot_empties_run_and_publish_restores_views(self):
        creds.write('tgbot_secret', 'evt')
        self.root.joinpath('clash-secret.txt').write_text('clash\n')
        shutil.rmtree(self.root / 'run')
        self.assertEqual(creds.read('tgbot_secret'), 'evt', 'нет вида - секрет расшифровывается из хранилища')
        creds.publish()
        self.assertEqual((self.root / 'run/panel/tgbot_secret').read_text(), 'evt')
        self.assertEqual((self.root / 'run/panel/clash_secret').read_text(), 'clash')
        self.assertEqual((self.root / 'run/inbox').stat().st_mode & 0o777, 0o770)

    def test_key_file_republishes_after_reboot(self):
        creds.write('mgmt_key', 'PRIVATE\n')
        shutil.rmtree(self.root / 'run')
        path = creds.key_file('mgmt_key')
        self.assertEqual((path.read_text(), path.stat().st_mode & 0o777), ('PRIVATE\n', 0o600))

    def test_digest_follows_the_view(self):
        creds.write('telegram_bot_token', 'a')
        before = creds.digest('bot')
        creds.write('telegram_bot_token', 'b')
        self.assertNotEqual(creds.digest('bot'), before)

    def test_unknown_name_is_refused(self):
        with self.assertRaises(creds.CredsError):
            creds.write('admin_password', 'x')

    def test_ensure_creates_once(self):
        first = creds.ensure('session_secret', 64)
        self.assertEqual(len(first), 64)
        self.assertEqual(creds.ensure('session_secret', 64), first)

    def test_bundle_carries_machine_keys_and_they_win_on_a_new_server(self):
        creds.write('backup_passphrase', 'pass')
        creds.write('orchestrator_key', 'OLD\n')
        creds.write('mgmt_key', 'MGMT\n')
        creds.bundle(self.root / 'secrets.json')
        self.assertEqual(json.loads((self.root / 'secrets.json').read_text()),
                         {'backup_passphrase': 'pass', 'orchestrator_key': 'OLD\n', 'mgmt_key': 'MGMT\n'})
        # Новый сервер: своё хранилище, ./deploy.sh -key уже создал свой ключ; экзиты знают только ключ архива.
        shutil.rmtree(self.root / 'store')
        shutil.rmtree(self.root / 'run')
        creds.write('orchestrator_key', 'NEW\n')
        replaced = creds.unbundle(self.root / 'secrets.json')
        self.assertEqual([creds.read(name) for name in ('backup_passphrase', 'orchestrator_key', 'mgmt_key')],
                         ['pass', 'OLD\n', 'MGMT\n'])
        self.assertEqual(replaced, ['orchestrator_key'], 'mgmt_key не было - заменять нечего')
        self.assertIn('экзиты знают ключи из архива', creds.replaced_note(replaced))
        self.assertEqual((creds.unbundle(self.root / 'secrets.json'), creds.replaced_note([])), ([], ''))


class InboxTests(Store):
    def setUp(self):
        super().setUp()
        creds.publish()
        self.inbox = self.root / 'run/inbox'

    def test_panel_secret_moves_to_the_store(self):
        (self.inbox / 'telegram_bot_token').write_text('9:new\n')
        creds.take_inbox()
        self.assertEqual(creds.read('telegram_bot_token'), '9:new')
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_symlink_and_foreign_names_are_dropped_unread(self):
        (self.root / 'shadow').write_text('root secret')
        (self.inbox / 'sso_client_secret').symlink_to(self.root / 'shadow')
        (self.inbox / 'orchestrator_key').write_text('planted')
        creds.take_inbox()
        self.assertEqual((creds.read('sso_client_secret'), creds.read('orchestrator_key')), ('', ''))
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_half_written_file_waits(self):
        (self.inbox / '.telegram_bot_token').write_text('partial')
        creds.take_inbox()
        self.assertTrue((self.inbox / '.telegram_bot_token').exists())


class AdoptTests(Store):
    def place(self, file, text):
        path = self.root / 'fs' / file.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_legacy_files_move_into_the_store_and_go(self):
        files = [self.place('/etc/anysda/session-secret.txt', 'sess\n'), self.place(os.path.expanduser('~/.ssh/id_ed25519'), 'KEY\n'),
                 self.place('/etc/anysda/admin-password.txt', 'admin pw'), self.place('/etc/anysda/adguard-password.txt', 'agh')]
        runtime = self.place('/var/lib/anysda-vpn2/runtime/telegram-runtime.json',
                             json.dumps({'bot_token': 'panel:tok', 'chat_id': '42'}) + '\n')
        runtime.chmod(0o640)
        taken = self.place('/etc/anysda/telegram-from-config.json', json.dumps({'bot_token': 'old:tok', 'chat_id': '42'}))
        creds.adopt(self.root / 'fs')
        self.assertEqual((creds.read('session_secret'), creds.read('orchestrator_key')), ('sess', 'KEY\n'))
        self.assertEqual(creds.read('telegram_bot_token'), 'panel:tok', 'действовал токен из панели')
        self.assertTrue(all(not path.exists() for path in files))
        # Запись та же, что у деплоя: перевод строки в конце - как был, файл чистой установки совпадает побайтно.
        self.assertEqual(runtime.read_text(), '{"chat_id": "42"}\n')
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o640)
        self.assertEqual(taken.read_text(), '{"chat_id": "42"}')
        creds.adopt(self.root / 'fs')
        self.assertEqual(creds.read('telegram_bot_token'), 'panel:tok', 'повтор ничего не меняет')


LEGACY = """# шапка
telegram:
  bot_token: '1:tok'   # бот
  chat_id: '42'
backup:
  enabled: true
  passphrase: pass
  s3:
    access_key: AK
    secret_key: SK
    bucket: b
admin:
  user: anysda
  password: adm

# orchestrator SSH-ключ — НЕ удалять: нужен для повторного деплоя
# с пересозданной entry-ноды. config.yaml надо сохранять/переносить.
orchestrator_key: S0VZCg==
orchestrator_pubkey: ssh-ed25519 AAAA anysda-orchestrator
"""
AREA = """entry:
  host: 192.0.2.1   # хаб
secrets:
  telegram_bot_token: '2:new'   # новый
  s3_access_key:
  admin_password: "p w"
exits:
  - tag: nl
    host: 192.0.2.2
"""
WIPED = AREA.replace("  telegram_bot_token: '2:new'   # новый", '  telegram_bot_token: # новый').replace(
    '  admin_password: "p w"', '  admin_password:')


class ConfigAreaTests(unittest.TestCase):
    def write(self, text):
        path = Path(tempfile.mkdtemp()) / 'config.yaml'
        self.addCleanup(shutil.rmtree, path.parent, True)
        path.write_text(text)
        path.chmod(0o600)
        return path

    def test_area_and_legacy_places_are_read(self):
        found = config.secrets(config.loads(LEGACY))
        self.assertEqual(found, {'telegram_bot_token': '1:tok', 'backup_passphrase': 'pass', 's3_access_key': 'AK',
                                 's3_secret_key': 'SK', 'admin_password': 'adm', 'orchestrator_key': 'KEY\n'})
        self.assertEqual(config.secrets(config.loads(AREA)), {'telegram_bot_token': '2:new', 'admin_password': 'p w'})

    def test_wipe_blanks_area_keeps_comments_and_second_run_is_noop(self):
        path = self.write(AREA)
        self.assertTrue(config.wipe_secrets(path))
        self.assertEqual(path.read_text(), WIPED)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        stat = path.stat()
        self.assertFalse(config.wipe_secrets(path))
        self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (stat.st_ino, stat.st_mtime_ns))
        self.assertEqual(config.secrets(config.load(path)), {})

    def test_wipe_keeps_named_secrets(self):
        path = self.write(AREA)
        self.assertTrue(config.wipe_secrets(path, keep=('admin_password',)))
        self.assertEqual(config.secrets(config.load(path)), {'admin_password': 'p w'})
        path = self.write(LEGACY)
        config.wipe_secrets(path, keep=('admin_password',))
        self.assertEqual(config.secrets(config.load(path)), {'admin_password': 'adm'})

    def test_wipe_removes_legacy_places_and_adds_the_area(self):
        path = self.write(LEGACY)
        self.assertTrue(config.wipe_secrets(path))
        text = path.read_text()
        cfg = config.load(path)
        self.assertEqual(config.secrets(cfg), {})
        self.assertEqual(cfg.raw['telegram'], {'chat_id': '42'})
        self.assertEqual(cfg.raw['backup'], {'enabled': 'true', 's3': {'bucket': 'b'}})
        self.assertNotIn('orchestrator', text)
        self.assertTrue(text.startswith('# шапка\n'))
        self.assertIn('\n'.join(config.AREA), text)
        self.assertFalse(config.wipe_secrets(path))


class TakeSecretsTests(Store):
    """Порядок: значение области сильнее хранилища, место 1.2.0 - только когда в хранилище пусто."""
    def take(self, text):
        path = self.root / 'config.yaml'
        path.write_text(text)
        with mock.patch.object(deploy, 'CONFIG', path), mock.patch.object(creds, 'adopt', lambda: None):
            return deploy.take_secrets()

    def test_precedence_and_password_stays_out_of_the_store(self):
        creds.write('telegram_bot_token', 'panel:tok')
        self.assertEqual(self.take(LEGACY), 'adm')
        self.assertEqual((creds.read('telegram_bot_token'), creds.read('backup_passphrase')), ('panel:tok', 'pass'))
        self.assertEqual(creds.read('orchestrator_key'), 'KEY\n')
        self.assertEqual(self.take(AREA), 'p w')
        self.assertEqual(creds.read('telegram_bot_token'), '2:new')
        self.assertNotIn('admin_password', [path.stem for path in (self.root / 'store').iterdir()])
        self.assertEqual(self.take(WIPED), '')
        self.assertEqual(creds.read('telegram_bot_token'), '2:new', 'пустая область - прежний секрет')


def keypair(folder, name):
    """Настоящий ключ ed25519: открытую часть деплой выводит ssh-keygen -y."""
    subprocess.run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-q', '-C', name, '-f', folder / name], check=True)
    return (folder / name).read_text(), (folder / f'{name}.pub').read_text()


class KeysFollowTheStoreTests(Store):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.root / 'home')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.keys = self.root / 'keys'
        self.keys.mkdir()

    def test_restored_key_replaces_the_public_part_and_its_old_line(self):
        deploy.orchestrator_key(create=True)
        fresh = deploy.key_id(deploy.pub().read_text())
        authorized = deploy.pub().parent / 'authorized_keys'
        authorized.write_text(authorized.read_text() + 'ssh-ed25519 AAAAhuman admin\n')
        old, old_pub = keypair(self.keys, 'old')
        creds.write('orchestrator_key', old)
        deploy.orchestrator_key(create=True)
        self.assertEqual(deploy.key_id(deploy.pub().read_text()), deploy.key_id(old_pub))
        lines = authorized.read_text()
        self.assertIn(deploy.key_id(old_pub), lines)
        self.assertIn('AAAAhuman', lines, 'ключ человека остаётся')
        self.assertNotIn(fresh, lines, 'строка прежнего ключа не сойдёт за ключ человека')


class NewServerRestoreTests(Store):
    """restore на новом сервере: ключи архива - в хранилище до деплоя, деплой входит ими на экзиты."""
    def archive(self, folder, members):
        name = folder / 'anysda-vpn2-2026-11-20T000000Z.tar.gz.age'
        with tarfile.open(name, 'w:gz') as tar:
            for member, data in members.items():
                info = tarfile.TarInfo(f'anysda-vpn2-2026-11-20T000000Z/{member}')
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return name

    def fresh(self, members, *args, typed='AGE-SECRET-KEY-1A'):
        """Новый сервер: архив выбран, ключ спрошен один раз (restore_input), секреты архива - в хранилище."""
        from vpn2.deploy import report
        local = self.root / 'backups'
        local.mkdir(exist_ok=True)
        cfg = self.root / 'config.yaml'
        cfg.write_text(ROOT.joinpath('config.example.yaml').read_text().replace(
            '    dir: /var/backups/anysda-vpn2', f'    dir: {local}'))
        name = self.archive(local, members)
        out, deploy = io.StringIO(), SimpleNamespace(restore_pulled=None)
        decrypt = lambda src, dst, secret, what: secret == 'AGE-SECRET-KEY-1A' and what == 'key' and shutil.copy(src, dst)  # noqa: E731
        with mock.patch.object(report, 'CONFIG', cfg), mock.patch.object(report, 'install_prereqs'), \
                mock.patch.object(report.archive, 'decrypt', decrypt), \
                mock.patch.object(report.keys, 'kind', lambda _path: 'key'), \
                mock.patch('sys.stdin', io.StringIO(typed + '\n')), contextlib.redirect_stdout(out):
            self.assertEqual(report.restore_input(deploy, list(args), True), [str(name)])
            self.assertEqual(deploy.restore_secret, (typed, 'key'))
            report.fresh_secrets(deploy, str(name))
        return out.getvalue()

    def test_archive_keys_win_over_a_key_from_deploy_key(self):
        creds.write('orchestrator_key', 'NEW\n')
        said = self.fresh({'secrets.json': json.dumps({'orchestrator_key': 'OLD\n', 'mgmt_key': 'M\n'}).encode()}, 'latest')
        self.assertEqual((creds.read('orchestrator_key'), creds.read('mgmt_key')), ('OLD\n', 'M\n'))
        self.assertEqual(said.count('экзиты знают ключи из архива'), 1)

    def test_archive_of_1_2_0_keeps_the_key_from_its_config(self):
        creds.write('orchestrator_key', 'KEY\n')
        self.fresh({'manifest.json': b'{}'})
        self.assertEqual(creds.read('orchestrator_key'), 'KEY\n')

    def test_new_server_takes_secrets_then_deploys_then_restores(self):
        from vpn2.deploy import __main__ as main
        calls, real = [], main.dispatch
        with mock.patch.object(main.report, 'restore_input', lambda _d, args, fresh: calls.append((args, fresh)) or ['/x/a.age', *args[1:]]), \
                mock.patch.object(main.report, 'fresh_secrets', lambda _d, path: calls.append(path)), \
                mock.patch.object(main, 'installed', lambda: False), \
                mock.patch.object(main, 'admin_key_check', lambda _env: calls.append('ключ человека')), \
                mock.patch.object(main, 'dispatch', lambda _d, argv, new_server=None: calls.append((argv, new_server)) or 0):
            self.assertEqual(real(SimpleNamespace(env=dict), ['restore', 'latest', '--force', '--allow-incompatible']), 0)
        self.assertEqual(calls, ['ключ человека', (['latest', '--force', '--allow-incompatible'], True), '/x/a.age', ([], None),
                                 (['restore', '/x/a.age', '--force', '--allow-incompatible'], False)])
