"""Хранилище секретов entry (vpn2.creds) и область secrets config.yaml: запись, виды, перенос мест 1.2.0, стирание."""
import grp
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from vpn2 import config, creds, deploy

GROUP = grp.getgrgid(os.getgid()).gr_name


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

    def test_bundle_carries_secrets_but_not_machine_keys(self):
        creds.write('backup_passphrase', 'pass')
        creds.write('orchestrator_key', 'OLD\n')
        creds.bundle(self.root / 'secrets.json')
        self.assertEqual(json.loads((self.root / 'secrets.json').read_text()), {'backup_passphrase': 'pass'})
        # Новый сервер: своё хранилище и свой ключ; restore шифрует секреты им, ключ ssh остаётся новым.
        shutil.rmtree(self.root / 'store')
        shutil.rmtree(self.root / 'run')
        creds.write('orchestrator_key', 'NEW\n')
        (self.root / 'secrets.json').write_text(json.dumps({'backup_passphrase': 'pass', 'orchestrator_key': 'OLD\n'}))
        creds.unbundle(self.root / 'secrets.json')
        self.assertEqual((creds.read('backup_passphrase'), creds.read('orchestrator_key')), ('pass', 'NEW\n'))


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
                             json.dumps({'bot_token': 'panel:tok', 'chat_id': '42'}))
        runtime.chmod(0o640)
        taken = self.place('/etc/anysda/telegram-from-config.json', json.dumps({'bot_token': 'old:tok', 'chat_id': '42'}))
        creds.adopt(self.root / 'fs')
        self.assertEqual((creds.read('session_secret'), creds.read('orchestrator_key')), ('sess', 'KEY\n'))
        self.assertEqual(creds.read('telegram_bot_token'), 'panel:tok', 'действовал токен из панели')
        self.assertTrue(all(not path.exists() for path in files))
        self.assertEqual(json.loads(runtime.read_text()), {'chat_id': '42'})
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o640)
        self.assertEqual(json.loads(taken.read_text()), {'chat_id': '42'})
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
