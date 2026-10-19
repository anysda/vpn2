"""Emergency panel regression contract, including real local TLS requests."""

import copy
from datetime import datetime, timezone
from contextlib import redirect_stderr
import hashlib
import http.client
import importlib
import inspect
import io
import json
import logging
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('EMERGENCY_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'infra/emergency'))


class EmergencyWebTests(unittest.TestCase):
    def setUp(self):
        self.web = importlib.import_module('vpn2_emergency.web')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.snapshot = dict(schema=1, version=7, created_at=datetime.now(timezone.utc).isoformat(),
                             bot_token='SNAPSHOT_BOT_SECRET', admin_chat_id=123456,
                             exits=[dict(id='one', name='Первый', ip='127.0.0.1'),
                                    dict(id='two', name='Второй', ip='203.0.113.2')],
                             clients=[dict(id=1, name='Тестовый клиент', telegram_chat_id=654321,
                                           allowed=True, expires_at=None,
                                           devices=[dict(id=11, name='одинаковое'), dict(id=12, name='одинаковое')])])
        self.file = self.directory / 'profile.txt'
        self.uri = 'hysteria2://test-credential@203.0.113.2:443/'
        self.file.write_text(self.uri)
        self.profiles = {11: dict(uri=self.uri, qr_png=b'PNG-test', file=str(self.file))}
        self.connections = {}
        self.queued = []
        row = dict(emergency='event', generation=1, client=1, version='v', event='initial', reason='',
                   attempts=0, due=0, sent=0, total=1, bot_token='LEAK')
        self.rows = [row | dict(state='sent', sent=1), row | dict(state='retry', reason='Telegram 503'),
                     row | dict(state='blocked', reason='автодоставка невозможна: нет токена бота'),
                     row | dict(state='pending'), row | dict(state='blocked', reason='нет канала доставки'),
                     row | dict(state='blocked', reason='клиент заморожен'), row | dict(state='cancelled'),
                     row | dict(state='sent', generation=2), row | dict(state='sent', emergency='old')]
        self.options = dict(exit_id='one', public_ip='127.0.0.1', snapshot_loader=lambda _p: copy.deepcopy(self.snapshot),
                            profiles=lambda _g: self.profiles, connections=lambda _g: self.connections,
                            delivery=lambda _g: self.rows,
                            broadcast=lambda message, generation: self.queued.append((message, generation)))
        self.panel = self.web.Panel(self.directory, **self.options)

    def start(self, cert_dir=None):
        static = ROOT / 'infra/emergency/vpn2_emergency/static'
        if not static.is_dir():
            static = self.directory / 'static'
            static.mkdir()
            (static / 'index.html').write_text('local panel')
            (static / 'instructions.html').write_bytes((ROOT / 'app/emergency/instructions.html').read_bytes())
        server = self.web.make_server(self.directory, address=('127.0.0.1', 0), static_dir=static,
                                      cert_dir=cert_dir or self.directory / 'missing', **self.options)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.server = server
        self.panel = server.panel
        self.panel.on_emergency_leader('event', 1)

    def request(self, path, body=None, cookie=None, origin=True):
        connection = http.client.HTTPSConnection('127.0.0.1', self.server.server_port,
                                                context=ssl._create_unverified_context(), timeout=5)
        headers = {'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        if origin:
            headers['Origin'] = f'https://127.0.0.1:{self.server.server_port}'
        connection.request('POST' if body is not None else 'GET', path,
                           json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def login(self):
        status, headers, _ = self.request('/api/login', dict(password=self.panel.auth['password']))
        self.assertEqual(status, 200)
        return headers['Set-Cookie']

    def test_password_entropy_and_groups(self):
        passwords = {self.web.generate_password() for _ in range(1000)}
        self.assertEqual(len(passwords), 1000)
        for password in passwords:
            self.assertRegex(password, r'^[A-Za-z0-9]{30}$')
            for group in (r'[A-Z]', r'[a-z]', r'[0-9]'):
                self.assertRegex(password, group)

    def test_tenth_failure_blocks_even_correct_password_and_restart(self):
        self.panel.on_emergency_leader('event', 1)
        for _ in range(9):
            self.assertEqual(self.panel.login('wrong')[0], 401)
        self.assertEqual(self.panel.login('wrong')[0], 429)
        self.assertEqual(self.panel.login(self.panel.auth['password'])[0], 429)
        resumed = self.web.Panel(self.directory, **self.options)
        self.assertEqual(resumed.login(resumed.auth['password'])[0], 429)
        resumed.auth['attempts']['local'][1] = time.time() - 1
        self.assertEqual(resumed.login(resumed.auth['password'])[0], 200)

    def test_attempt_limit_is_per_address_so_admin_is_not_locked_out(self):
        self.panel.on_emergency_leader('event', 1)
        for _ in range(10):
            self.panel.login('wrong', '198.51.100.7')
        self.assertEqual(self.panel.login(self.panel.auth['password'], '198.51.100.7')[0], 429)
        self.assertEqual(self.panel.login(self.panel.auth['password'], '203.0.113.9')[0], 200)
        for index in range(self.web.TRACKED_ADDRESSES + 50):
            self.panel.login('wrong', f'2001:db8::{index:x}')
        self.assertLessEqual(len(self.panel.auth['attempts']), self.web.TRACKED_ADDRESSES)
        self.assertLess(self.panel.auth_file.stat().st_size, 128 * 1024)
        print('limit: 10 wrong from one address -> 429 there, admin from another address -> 200')

    def test_delivery_counts_current_generation_queue_rows(self):
        self.panel.on_emergency_leader('event', 1)
        self.assertEqual(self.panel.view()['delivery'],
                         dict(sent=1, pending=1, errors=2, without_telegram=1, denied=1))

    def test_password_survives_restart_but_not_role_change(self):
        first = self.panel.on_emergency_leader('event', 1)
        resumed = self.web.Panel(self.directory, **self.options)
        self.assertEqual(first, resumed.on_emergency_leader('event', 1))
        self.assertEqual(self.panel.auth_file.stat().st_mode & 0o777, 0o600)
        resumed.on_step_down('two')
        self.assertEqual(resumed.login(first['panel_password'])[0], 503)
        self.assertNotEqual(first['panel_password'], resumed.on_emergency_leader('event', 2)['panel_password'])

    def test_no_ten_hour_expiration(self):
        self.panel.on_emergency_leader('event', 1)
        with patch.object(self.web.time, 'time', return_value=time.time() + 11 * 3600):
            self.assertEqual(self.panel.login(self.panel.auth['password'])[0], 200)
        self.assertNotIn('expires_at', self.panel.auth)

    def test_connection_requires_authenticated_current_generation(self):
        self.panel.on_emergency_leader('event', 1)
        self.assertEqual(self.panel.view()['clients'][0]['status'], 'unknown')
        self.connections.update({11: 'never', 12: 'never'})
        self.assertEqual(self.panel.view()['clients'][0]['status'], 'never')
        self.connections[11] = 'connected'
        self.assertEqual(self.panel.view()['clients'][0]['status'], 'connected')
        self.connections[11] = 'seen'
        self.assertEqual(self.panel.view()['clients'][0]['status'], 'seen')

    def test_snapshot_allowlist_and_original_device_names(self):
        self.panel.on_emergency_leader('event', 1)
        state = self.panel.view()
        self.assertEqual([d['name'] for d in state['clients'][0]['devices']], ['одинаковое', 'одинаковое'])
        self.assertEqual([d['ready'] for d in state['clients'][0]['devices']], [True, False])
        for value in ('SNAPSHOT_BOT_SECRET', '123456', '654321', 'LEAK', 'bot_token'):
            self.assertNotIn(value, json.dumps(state))
        self.assertIsNotNone(state['snapshot_age'])

    def test_denied_expired_invalid_expiry_never_expose_profiles(self):
        self.panel.on_emergency_leader('event', 1)
        self.assertIsNotNone(self.panel.profile(11))
        for expiry in ('2000-01-01T00:00:00Z', 'invalid'):
            self.snapshot['clients'][0]['expires_at'] = expiry
            self.assertIsNone(self.panel.profile(11))
        self.snapshot['clients'][0].update(expires_at=None, allowed=False)
        self.assertIsNone(self.panel.profile(11))

    def test_network_partition_recovery_and_cancel(self):
        self.panel.on_emergency_leader('event', 2, True)
        self.assertTrue(self.panel.view()['autonomous'])
        self.panel.on_entry_recovered(time.monotonic() + 600)
        self.assertIn(self.panel.view()['remaining'], (599, 600))
        resumed = self.web.Panel(self.directory, **self.options)
        self.assertIn(resumed.view()['remaining'], (599, 600))
        self.panel.on_return_cancelled()
        self.assertTrue(self.panel.view()['cancelled'])
        self.assertIsNone(self.panel.view()['remaining'])
        self.panel.on_emergency_end()
        self.panel.on_return_cancelled()
        self.panel.on_entry_recovered(time.monotonic() + 600)
        self.assertFalse(self.panel.auth)

    def test_panel_shows_the_deadline_from_the_notices(self):
        self.panel.on_emergency_leader('event', 2)
        agreed = (int(time.time()) // 60 + 11) * 60
        self.panel.on_entry_recovered(time.monotonic() + agreed - time.time(), agreed)
        self.assertEqual(self.panel.view()['deadline'], agreed)
        self.assertEqual(self.web.Panel(self.directory, **self.options).view()['deadline'], agreed)

    def test_https_login_cookie_and_revocation(self):
        self.start()
        self.assertEqual(self.request('/api/state')[0], 401)
        cookie = self.login()
        for attribute in ('Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(attribute, cookie)
        self.assertEqual(self.request('/api/state', cookie=cookie)[0], 200)
        self.panel.on_step_down()
        self.assertFalse(self.panel.authenticated(cookie))
        self.panel.on_emergency_leader('event', 2)
        self.assertEqual(self.request('/api/state', cookie=cookie)[0], 401)

    def test_https_tenth_failure(self):
        self.start()
        statuses = [self.request('/api/login', dict(password='wrong'))[0] for _ in range(10)]
        self.assertEqual(statuses, [401] * 9 + [429])
        print('HTTPS login: 9 × 401, 10th = 429; correct password blocked for 300s')

    def test_fallback_fingerprint_and_ip_certificate(self):
        self.start()
        cert = self.directory / 'web-tls/cert.pem'
        expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest().upper()
        message = self.panel.on_emergency_leader('event', 1)
        self.assertEqual(message['certificate_fingerprint'], expected)
        tgqueue = importlib.import_module('vpn2_emergency.tgqueue')
        accepted = inspect.signature(tgqueue.enqueue_admin).parameters
        self.assertLessEqual(set(message), set(accepted))
        self.assertTrue(message['panel_ready'])
        trusted = ssl.create_default_context(cafile=str(cert))
        conn = http.client.HTTPSConnection('127.0.0.1', self.server.server_port, context=trusted)
        conn.request('GET', '/instructions')
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()

    def test_renewed_certificate_reaches_new_connections(self):
        # VPN2-529: the panel takes a renewed IP certificate without a restart.
        source = self.directory / 'ip'
        source.mkdir()

        def issue(name):
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                            '-nodes', '-days', '1', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                            '-keyout', str(self.directory / f'{name}.key'), '-out', str(self.directory / f'{name}.pem')],
                           check=True, capture_output=True)
            return ssl.PEM_cert_to_DER_cert((self.directory / f'{name}.pem').read_text())

        def publish(cert, key):
            (source / 'fullchain.pem').write_bytes((self.directory / f'{cert}.pem').read_bytes())
            (source / 'privkey.pem').write_bytes((self.directory / f'{key}.key').read_bytes())

        def served():
            raw = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=5)
            with ssl._create_unverified_context().wrap_socket(raw) as connection:
                return connection.getpeercert(binary_form=True)

        first, second = issue('first'), issue('second')
        publish('first', 'first')
        self.start(cert_dir=source)
        self.assertEqual(served(), first)
        self.assertEqual(self.panel.fingerprint, hashlib.sha256(first).hexdigest().upper())
        self.assertFalse(self.server.refresh_tls())
        # A pair read across the renewal does not load; the settled pair does.
        publish('second', 'first')
        self.assertFalse(self.server.refresh_tls())
        self.assertEqual(served(), first)
        publish('second', 'second')
        self.assertTrue(self.server.refresh_tls())
        self.assertEqual(served(), second)
        message = self.panel.on_emergency_leader('event', 1)
        self.assertEqual(message['certificate_fingerprint'], hashlib.sha256(second).hexdigest().upper())
        self.assertFalse(self.server.refresh_tls())
        self.assertEqual(sorted(path.name for path in (self.directory / 'web-tls').iterdir()), [])

    def test_rejected_certificate_logs_one_line_without_traceback(self):
        self.start()
        stderr = io.StringIO()
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        log = logging.getLogger('vpn2_emergency.web')
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        level = log.level
        log.setLevel(logging.INFO)
        self.addCleanup(log.setLevel, level)
        with redirect_stderr(stderr):
            # A verifying client, as curl without -k: it refuses the fallback certificate.
            conn = http.client.HTTPSConnection('127.0.0.1', self.server.server_port,
                                               context=ssl.create_default_context(), timeout=5)
            with self.assertRaises(ssl.SSLCertVerificationError):
                conn.request('GET', '/')
            conn.close()
            deadline = time.monotonic() + 5
            while not stderr.getvalue() and not records and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertNotIn('Traceback', stderr.getvalue())
        self.assertEqual([record.getMessage() for record in records],
                         [f'emergency panel: connection from 127.0.0.1 dropped: {records[0].args[1]}'])
        print('rejected certificate:', records[0].getMessage())
        # The panel keeps serving.
        self.assertEqual(self.request('/instructions')[0], 200)

    def test_post_requires_same_origin_json_and_session(self):
        self.start()
        self.assertEqual(self.request('/api/login', dict(password='wrong'), origin=False)[0], 403)
        self.assertEqual(self.request('/api/broadcast', dict(text='hello'))[0], 401)
        cookie = self.login()
        self.assertEqual(self.request('/api/broadcast', dict(text=' hello '), cookie)[0], 202)
        self.assertEqual(self.queued, [('hello', 1)])
        self.assertEqual(self.request('/api/broadcast', dict(text=' '), cookie)[0], 400)
        self.assertEqual(self.request('/api/logout', {}, cookie)[0], 200)
        self.assertEqual(self.request('/api/state', cookie=cookie)[0], 401)

    def test_profile_id_access_and_same_material(self):
        self.start()
        self.assertEqual(self.request('/api/profile/11/file')[0], 401)
        cookie = self.login()
        self.assertEqual(json.loads(self.request('/api/profile/11/uri', cookie=cookie)[2])['uri'], self.uri)
        self.assertEqual(self.request('/api/profile/11/file', cookie=cookie)[2], self.uri.encode())
        self.assertEqual(self.request('/api/profile/11/qr', cookie=cookie)[2], b'PNG-test')
        self.assertEqual(self.request('/api/profile/12/file', cookie=cookie)[0], 404)
        self.profiles[11]['file'] = str(ROOT / 'config.example.yaml')
        self.assertEqual(self.request('/api/profile/11/file', cookie=cookie)[0], 404)

    def test_instructions_local_public_without_snapshot_even_after_stepdown(self):
        self.start()
        for transition in (lambda: None, lambda: self.panel.on_entry_recovered(time.monotonic() + 600),
                           lambda: self.panel.on_step_down('two')):
            transition()
            status, headers, html = self.request('/instructions')
            self.assertEqual(status, 200)
            self.assertNotIn('Location', headers)
            for secret in ('Тестовый клиент', 'одинаковое', 'SNAPSHOT_BOT_SECRET', '654321', self.uri):
                self.assertNotIn(secret, html.decode())
        self.assertEqual(self.request('/')[1]['Location'], 'https://203.0.113.2/')
        self.assertNotEqual(self.request('/instuctions')[0], 200)
        self.snapshot.clear()
        self.assertEqual(self.request('/instructions')[0], 200)
        print('/instructions: HTTPS 200, local on follower, no snapshot data; / → leader')


if __name__ == '__main__':
    unittest.main(verbosity=2)
