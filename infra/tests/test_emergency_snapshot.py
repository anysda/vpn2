"""Emergency client snapshot: panel access model, versions, delivery over SSH.

The panel database is built from the real panel migrations. The same
assertions can target an older tree using VPN2_TEST_ROOT.
"""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
MIGRATIONS = Path(__file__).resolve().parents[2] / 'app/server/database/migrations'
sys.path.insert(0, str(ROOT / 'infra/emergency'))
from vpn2_emergency import snapshot, snapshot_sender  # noqa: E402

INSTALLATION = 'test-installation'
TOKEN = 'test-bot-token-secret'
PASSWORD = 'panel-client-password'
# The exit applies expiry with its own clock: scenarios run in the recent past.
NOW = int(time.time()) - 10_000
AGENT = {'installation_id': INSTALLATION, 'node': '__entry__', 'entry_id': '__entry__',
         'nodes': {'__entry__': {'host': '192.0.2.1', 'port': 51821},
                   'exit-a': {'host': '192.0.2.2', 'port': 51821},
                   'exit-b': {'host': '192.0.2.3', 'port': 51821}}}


class Panel:
    """A panel database with the real schema and the access operation model."""

    def __init__(self, root):
        self.path = root / 'db.sqlite'
        self.telegram = root / 'telegram-runtime.json'
        self.telegram.write_text(json.dumps({'bot_token': TOKEN, 'chat_id': '-100123',
                                             'admin_username': 'admin'}))
        self.operation = 0
        with self.db() as db:
            journal = json.loads((MIGRATIONS / 'meta/_journal.json').read_text())
            for entry in journal['entries']:
                sql = (MIGRATIONS / f"{entry['tag']}.sql").read_text()
                for statement in sql.split('--> statement-breakpoint'):
                    if statement.strip():
                        db.execute(statement)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            yield db

    def client(self, client_id, name, enabled=True, **columns):
        with self.db() as db:
            db.execute('INSERT INTO clients (id, name, password, tg_chat_id) VALUES (?, ?, ?, ?)',
                       (client_id, name, PASSWORD, str(1000 + client_id)))
            for column, value in columns.items():
                db.execute(f'UPDATE clients SET {column} = ? WHERE id = ?', (value, client_id))
        if enabled is not None:
            self.op(client_id, 0, 'confirmed', enabled)

    def device(self, device_id, client_id, name, deleting=0):
        with self.db() as db:
            db.execute('INSERT INTO devices (id, client_id, name, deleting) VALUES (?, ?, ?, ?)',
                       (device_id, client_id, name, deleting))

    def op(self, client_id, version, status, enabled):
        self.operation += 1
        with self.db() as db:
            db.execute(
                'INSERT INTO access_operations (id, client_id, kind, version, idempotency_key, '
                'request_hash, status, protocols, payload, actor, next_retry, created_at) '
                "VALUES (?, ?, 'freeze', ?, ?, 'h', ?, '[]', ?, 'admin', 0, 0)",
                (f'op-{self.operation}', client_id, version, f'key-{self.operation}', status,
                 json.dumps({'enabled': enabled})))
            db.execute('UPDATE clients SET access_version = ? WHERE id = ?', (version, client_id))

    def edit(self, client_id, enabled, **columns):
        """What a panel freeze/unfreeze does: new current version, then confirmation."""
        with self.db() as db:
            version = db.execute('SELECT access_version FROM clients WHERE id = ?',
                                 (client_id,)).fetchone()[0] + 1
            db.execute("UPDATE access_operations SET status = 'superseded' WHERE client_id = ?",
                       (client_id,))
            for column, value in columns.items():
                db.execute(f'UPDATE clients SET {column} = ? WHERE id = ?', (value, client_id))
        self.op(client_id, version, 'confirmed', enabled)

    def set(self, sql, *params):
        with self.db() as db:
            db.execute(sql, params)


class Exits:
    """In-process exits: the same receiver code SSH runs on a real exit."""

    def __init__(self, root):
        self.root = root
        self.requests = []
        self.down = set()
        self.now = None

    def deliver(self, exit_id, request):
        self.requests.append((exit_id, request['op']))
        if exit_id in self.down:
            return {'status': 'unavailable'}
        stream = _Stream(snapshot.encode(request))
        return snapshot.receive(stream, self.root / exit_id, INSTALLATION, self.now)

    def load(self, exit_id):
        return snapshot.load(self.root / exit_id)


class _Stream:
    def __init__(self, raw):
        self.raw = raw

    def read(self, _size):
        return self.raw


def allowed(value):
    return {client['name']: client['allowed'] for client in value['clients']}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.panel = Panel(self.root)
        self.exits = Exits(self.root / 'exits')
        self.sender = self.root / 'sender'

    def run_once(self, now=NOW, sender=None):
        config = {'state_dir': sender or self.sender, 'agent': AGENT}
        self.exits.now = now
        return snapshot_sender.run_once(
            config, self.exits.deliver,
            lambda moment: snapshot_sender.collect(self.panel.path, self.panel.telegram, moment),
            now=now)

    def image(self, version=1, **extra):
        return {'schema': 1, 'version': version, 'installation_id': INSTALLATION,
                'created_at': snapshot.iso_time(NOW), 'admin_chat_id': None, 'bot_token': None,
                'exits': [{'id': 'exit-a', 'name': 'exit-a', 'ip': '192.0.2.2', 'priority': 0}],
                'clients': [{'id': 1, 'name': 'A', 'telegram_chat_id': None, 'allowed': True,
                             'expires_at': None, 'devices': []}]} | extra

    def test_contract_allows_new_fields(self):
        value = self.image(future={'x': 1})
        value['clients'][0]['note'] = 'new'
        value['clients'][0]['devices'] = [{'id': 7, 'name': 'phone', 'os': 'any'}]
        value['exits'][0]['region'] = 'eu'
        result = snapshot.accept(self.root / 'exit', value, None, INSTALLATION)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(snapshot.load(self.root / 'exit')['clients'][0]['note'], 'new')

    def test_old_snapshot_does_not_overwrite_new(self):
        state = self.root / 'exit'
        newer = self.image(version=5)
        self.assertEqual(snapshot.accept(state, newer, None, INSTALLATION)['status'], 'accepted')
        older = self.image(version=4)
        older['clients'][0]['allowed'] = False
        result = snapshot.accept(state, older, snapshot.digest(newer), INSTALLATION)
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(result['version'], 5)
        guess = self.image(version=6, bot_token='x')
        self.assertEqual(snapshot.accept(state, guess, None, INSTALLATION)['status'], 'conflict')
        self.assertEqual(snapshot.load(state)['version'], 5)
        self.assertEqual(snapshot.accept(state, self.image(version=9, installation_id='other'),
                                         snapshot.digest(newer), INSTALLATION)['status'],
                         'wrong-installation')

    def test_frozen_expired_deleting_and_unconfirmed_are_denied(self):
        self.panel.client(1, 'active')
        self.panel.client(2, 'frozen', frozen_manual=1)
        self.panel.client(3, 'expired', expires_at=NOW - 1)
        self.panel.client(4, 'deleting', deleting=1)
        self.panel.client(5, 'no-operation', enabled=None)
        self.panel.client(6, 'pending-enable', enabled=False)
        self.panel.op(6, 1, 'pending', True)
        self.panel.client(7, 'confirmed-disabled', enabled=False)
        self.panel.client(8, 'expires-later', expires_at=NOW + 1_000_000)
        self.panel.device(10, 1, 'Телефон', deleting=0)
        self.panel.device(11, 1, 'старый', deleting=1)
        self.assertEqual(set(self.run_once().values()), {'accepted'})
        value = self.exits.load('exit-a')
        self.assertEqual(allowed(value), {
            'active': True, 'frozen': False, 'expired': False, 'deleting': False,
            'no-operation': False, 'pending-enable': False, 'confirmed-disabled': False,
            'expires-later': True})
        self.assertEqual(value['clients'][0]['devices'], [{'id': 10, 'name': 'Телефон'}])
        self.assertEqual(value['clients'][0]['telegram_chat_id'], 1001)
        self.assertEqual((value['bot_token'], value['admin_chat_id']), (TOKEN, -100123))
        self.assertNotIn('access_version', value['clients'][0])
        self.assertNotIn(PASSWORD, json.dumps(value))
        self.assertEqual([n['id'] for n in value['exits']], ['exit-a', 'exit-b'])
        mode = (self.exits.root / 'exit-a' / snapshot.NAME).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_panel_change_reaches_every_exit(self):
        self.panel.client(1, 'Анна')
        self.panel.device(10, 1, 'iPhone Анны')
        self.run_once()
        self.panel.edit(1, False, frozen_manual=1, name='Анна (заморожена)')
        self.panel.set('UPDATE devices SET name = ? WHERE id = 10', 'Новое имя')
        self.assertEqual(self.run_once(NOW + 5), {'exit-a': 'accepted', 'exit-b': 'accepted'})
        for exit_id in ('exit-a', 'exit-b'):
            value = self.exits.load(exit_id)
            self.assertEqual(value['version'], 2)
            self.assertEqual(allowed(value), {'Анна (заморожена)': False})
            self.assertEqual(value['clients'][0]['devices'][0]['name'], 'Новое имя')

    def test_version_changes_only_on_change_and_age_counts_from_confirmation(self):
        self.panel.client(1, 'A')
        self.run_once()
        self.exits.requests.clear()
        self.assertEqual(self.run_once(NOW + 30), {'exit-a': 'unchanged', 'exit-b': 'unchanged'})
        self.assertEqual(self.exits.requests, [])
        self.assertEqual(self.run_once(NOW + 61), {'exit-a': 'unchanged', 'exit-b': 'unchanged'})
        self.assertEqual(self.exits.requests, [('exit-a', 'confirm'), ('exit-b', 'confirm')])
        value = self.exits.load('exit-a')
        self.assertEqual(value['version'], 1)
        self.assertEqual(value['created_at'], snapshot.iso_time(NOW))
        self.assertEqual(snapshot.timestamp(value['confirmed_at']), NOW + 61)

    def test_expiry_applies_on_exit_during_outage(self):
        self.panel.client(1, 'A', expires_at=NOW + 1000)
        self.run_once()
        # Allowed when sent; the entry is silent since, the exit clock is past the deadline.
        self.assertFalse(self.exits.load('exit-a')['clients'][0]['allowed'])
        image = json.loads((self.exits.root / 'exit-a' / snapshot.NAME).read_text())
        self.assertTrue(image['clients'][0]['allowed'])
        self.assertGreater(snapshot.age_seconds(self.exits.root / 'exit-a'), 9000)

    def test_restored_entry_continues_above_exit_and_keeps_widening_denied(self):
        self.panel.client(1, 'stays')
        self.panel.client(2, 'frozen-later')
        backup = self.root / 'backup.sqlite'
        with self.panel.db() as db, closing(sqlite3.connect(backup)) as copy:
            db.backup(copy)
        self.run_once()
        self.panel.edit(2, False, frozen_manual=1)
        self.panel.client(3, 'new-after-backup')
        self.run_once(NOW + 5)
        self.panel.edit(1, True, name='stays renamed')
        self.run_once(NOW + 10)
        self.assertEqual(self.exits.load('exit-a')['version'], 3)
        # Restore: old panel database and no sender state.
        with closing(sqlite3.connect(backup)) as copy, self.panel.db() as db:
            copy.backup(db)
        results = self.run_once(NOW + 20, sender=self.root / 'restored-sender')
        self.assertEqual(results, {'exit-a': 'accepted', 'exit-b': 'accepted'})
        for exit_id in ('exit-a', 'exit-b'):
            value = self.exits.load(exit_id)
            self.assertEqual(value['version'], 4)
            self.assertEqual(allowed(value), {'stays': True, 'frozen-later': False})
        # The hold stays until the client is edited in the panel.
        self.panel.client(4, 'added-after-restore')
        self.run_once(NOW + 30, sender=self.root / 'restored-sender')
        self.assertFalse(allowed(self.exits.load('exit-a'))['frozen-later'])
        self.assertTrue(allowed(self.exits.load('exit-a'))['added-after-restore'])
        self.panel.edit(2, True)
        self.assertEqual(self.run_once(NOW + 40, sender=self.root / 'restored-sender'),
                         {'exit-a': 'accepted', 'exit-b': 'accepted'})
        self.assertTrue(allowed(self.exits.load('exit-b'))['frozen-later'])

    def test_rolled_back_panel_database_holds_grants(self):
        self.panel.client(1, 'A', enabled=False)
        self.panel.edit(1, True)
        backup = self.root / 'backup.sqlite'
        with self.panel.db() as db, closing(sqlite3.connect(backup)) as copy:
            db.backup(copy)
        self.panel.edit(1, False, frozen_manual=1)
        self.run_once()
        with closing(sqlite3.connect(backup)) as copy, self.panel.db() as db:
            copy.backup(db)
        self.run_once(NOW + 5)
        self.assertFalse(allowed(self.exits.load('exit-a'))['A'])
        self.panel.edit(1, True)
        self.run_once(NOW + 10)
        self.assertTrue(allowed(self.exits.load('exit-a'))['A'])

    def test_unreachable_exit_catches_up_without_blocking(self):
        self.panel.client(1, 'A')
        self.exits.down.add('exit-b')
        self.assertEqual(self.run_once(), {'exit-a': 'accepted', 'exit-b': 'unavailable'})
        self.panel.edit(1, False, frozen_manual=1)
        self.run_once(NOW + 5)
        self.exits.down.clear()
        self.assertEqual(self.run_once(NOW + 10), {'exit-a': 'unchanged', 'exit-b': 'accepted'})
        self.assertEqual(self.exits.load('exit-b')['version'], 2)

    def test_insecure_state_is_not_loaded(self):
        self.panel.client(1, 'A')
        self.run_once()
        path = self.exits.root / 'exit-a' / snapshot.NAME
        path.chmod(0o640)
        self.assertIsNone(self.exits.load('exit-a'))

    def test_ssh_delivery_end_to_end_without_secrets_in_output(self):
        self.panel.client(1, 'A')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'ssh'
        fake.write_text(
            '#!/usr/bin/env python3\n'
            'import os, subprocess, sys\n'
            'args = sys.argv[1:]\n'
            "pinned = 'StrictHostKeyChecking=yes' in args and 'BatchMode=yes' in args\n"
            "if not pinned or args[:2] != ['-F', os.environ['FAKE_SSH_CONFIG']] \\\n"
            "        or args[-2] not in ('exit-a', 'exit-b'):\n"
            '    sys.exit(255)\n'
            "root = os.environ['FAKE_EXITS']\n"
            "sys.exit(subprocess.run([sys.executable, '-B', '-m', 'vpn2_emergency.snapshot',\n"
            "    'receive', '--config', root + '/agent.json', '--state-dir',\n"
            "    root + '/' + args[-2]], cwd=os.environ['FAKE_CODE']).returncode)\n")
        fake.chmod(0o700)
        agent = self.root / 'agent.json'
        agent.write_text(json.dumps(AGENT))
        exits = self.root / 'ssh-exits'
        exits.mkdir()
        (exits / 'agent.json').write_text(json.dumps({'installation_id': INSTALLATION}))
        env = os.environ | {'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                            'FAKE_EXITS': str(exits), 'FAKE_SSH_CONFIG': '/test/ssh.conf',
                            'FAKE_CODE': str(ROOT / 'infra/emergency')}
        command = [sys.executable, '-B', '-m', 'vpn2_emergency.snapshot_sender', 'run', '--once',
                   '--agent-config', str(agent), '--state-dir', str(self.sender),
                   '--database', str(self.panel.path), '--telegram-file',
                   str(self.panel.telegram), '--panel-user', '', '--ssh-config',
                   '/test/ssh.conf']
        result = subprocess.run(command, cwd=ROOT / 'infra/emergency', env=env,
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr),
                         (0, '{"accepted": 2}', ''))
        value = snapshot.load(exits / 'exit-b')
        self.assertEqual((value['version'], value['bot_token']), (1, TOKEN))
        self.panel.edit(1, False, frozen_manual=1)
        result = subprocess.run(command, cwd=ROOT / 'infra/emergency', env=env,
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.stdout.strip(), '{"accepted": 2}')
        self.assertFalse(snapshot.load(exits / 'exit-a')['clients'][0]['allowed'])
        sender_state = (self.sender / 'sender.json').read_text()
        self.assertNotIn(PASSWORD, sender_state)
        for output in (result.stdout, result.stderr):
            self.assertNotIn(TOKEN, output)
            self.assertNotIn(PASSWORD, output)


if __name__ == '__main__':
    unittest.main()
