"""Outgoing delivery against a local Bot API; no real Telegram token or polling."""

from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(os.environ.get('VPN2_TG_SOURCE', ROOT)) / 'infra/emergency'))
tgqueue = importlib.import_module('vpn2_emergency.tgqueue')
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
STAMP = '2026-01-01T00:00:00+00:00'
FOOTER = '\n\nУзел: exit-a. Время выдачи: 00:00 01.01.2026 (UTC+00:00)'


def load_snapshot(state_dir):
    # Contract stub only: the snapshot implementation belongs to its own module.
    return json.loads((Path(state_dir) / 'fixture.json').read_text())


def snapshot_stub():
    module = types.ModuleType('vpn2_emergency.snapshot')
    module.load = load_snapshot
    return patch.dict(sys.modules, {'vpn2_emergency.snapshot': module})


class BotServer(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(('127.0.0.1', 0), BotHandler)
        self.calls = []
        self.replies = {}
        self.hold_call = None
        self.arrived = threading.Event()
        self.release = threading.Event()


class BotHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):  # noqa - HTTP handler
        pass

    def do_POST(self):  # noqa - HTTP handler
        raw = self.rfile.read(int(self.headers['Content-Length']))
        if self.headers['Content-Type'].startswith('application/json'):
            data = json.loads(raw)
        else:
            message = BytesParser(policy=policy.default).parsebytes(
                f'Content-Type: {self.headers["Content-Type"]}\r\n\r\n'.encode() + raw)
            data = {p.get_param('name', header='Content-Disposition'):
                    p.get_payload(decode=True).decode() for p in message.iter_parts()}
            data['chat_id'] = int(data['chat_id'])
        self.server.calls.append((self.path.rsplit('/', 1)[-1], data))
        if len(self.server.calls) == self.server.hold_call:
            self.server.arrived.set()
            self.server.release.wait(10)
            return
        replies = self.server.replies.get(data['chat_id'], [])
        code, result = replies.pop(0) if replies else (200, {'ok': True, 'result': {'message_id': 1}})
        self.send_response(code)
        if code == 302:
            self.send_header('Location', self.server.redirect_url)
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


class TelegramQueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='tgqueue-')
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.snapshot = {'schema': 1, 'bot_token': '123:fixture-only', 'admin_chat_id': 900,
                         'clients': [
                             {'id': 1, 'name': 'first', 'telegram_chat_id': 101, 'allowed': True,
                              'expires_at': None, 'devices': [{'id': 11, 'name': 'телефон <&> 🦊'}]},
                             {'id': 2, 'name': 'second', 'telegram_chat_id': 102, 'allowed': True,
                              'expires_at': None, 'devices': [{'id': 22, 'name': 'телефон <&> 🦊'}]}]}
        self.save_snapshot()
        stub = snapshot_stub()
        stub.start()
        self.addCleanup(stub.stop)
        self.bot = BotServer()
        thread = threading.Thread(target=self.bot.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        self.addCleanup(self.bot.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.bot.shutdown)
        self.api = f'http://127.0.0.1:{self.bot.server_port}'
        self.base = {'emergency_id': 'outage-a', 'generation': 1, 'bundle_version': 'v1',
                     'exit_name': 'exit-a', 'issued_at': STAMP}
        self.profiles = {11: {'uri': 'hysteria2://first@203.0.113.1:443/'},
                         22: {'uri': 'hysteria2://second@203.0.113.1:443/'}}

    def save_snapshot(self):
        (self.state / 'fixture.json').write_text(json.dumps(self.snapshot))

    def enqueue(self, client_id=1, **kwargs):
        return tgqueue.enqueue_client(self.state, client_id=client_id,
                                      **(self.base | {'exit_url': 'https://203.0.113.1',
                                                     'profiles': self.profiles, 'now': NOW} | kwargs))

    def admin(self, **kwargs):
        return tgqueue.enqueue_admin(self.state, **(self.base | {
            'panel_url': 'https://203.0.113.1', 'panel_password': 'private-panel-fixture',
            'panel_ready': True} | kwargs))

    def tick(self, **kwargs):
        return tgqueue.run_once(self.state, **({'api_base': self.api, 'now': NOW} | kwargs))

    def drain(self):
        for _ in range(20):
            if not self.tick():
                break

    def status(self, audience='client'):
        return tgqueue.delivery_status(self.state, audience)

    def test_exact_initial_text_own_profiles_and_instruction(self):
        self.enqueue()
        self.enqueue(2)
        self.drain()
        for client, secret in ((101, 'first'), (102, 'second')):
            parts = [data for method, data in self.bot.calls if data['chat_id'] == client]
            expected = ('Основное VPN-подключение временно недоступно. Аварийный конфиг '
                        'Hysteria2 для устройства телефон <&> 🦊 — ниже. Как подключиться: '
                        'https://203.0.113.1/instructions. Сохраните прежний VPN-профиль '
                        '— он понадобится после восстановления')
            self.assertEqual(parts[0]['text'], expected + FOOTER)
            self.assertIn(f'://{secret}@', parts[1]['text'])
            self.assertNotIn('parse_mode', parts[0])
            self.assertEqual(len(parts), 2)
        self.assertEqual({method for method, data in self.bot.calls}, {'sendMessage'})
        print('delivery: each client received only own profile and /instructions; names unchanged')

    def test_process_restart_mid_bundle_and_dedup_key(self):
        self.enqueue()
        self.assertEqual(self.tick(max_messages=1), 1)
        result = subprocess.run([sys.executable, __file__, '--worker', str(self.state), self.api],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.enqueue()
        self.drain()
        self.assertEqual(len(self.bot.calls), 2)
        self.assertEqual(self.status()[0]['state'], 'sent')
        for changes in ({'bundle_version': 'v2'}, {'generation': 2}, {'emergency_id': 'outage-b'}):
            self.enqueue(**changes)
            self.drain()
        self.assertEqual(len(self.bot.calls), 8)
        print('restart: confirmed=1, remaining=1, duplicate confirmed parts=0; delivery key dimensions OK')

    def test_429_retry_after_persists_and_others_continue(self):
        self.enqueue()
        self.enqueue(2)
        self.bot.replies[101] = [(429, {'ok': False, 'error_code': 429,
                                       'parameters': {'retry_after': 37}})]
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.status()[0]['due'], NOW + 37)
        self.tick(now=NOW + 36)
        self.assertEqual(sum(d['chat_id'] == 101 for m, d in self.bot.calls), 1)
        self.tick(now=NOW + 37)
        self.tick(now=NOW + 38)
        self.assertTrue(all(s['state'] == 'sent' for s in self.status()))
        print('429: retry_after=37 respected; other recipient delivered without waiting')

    def test_unreachable_recipient_and_admin_error_are_separate(self):
        self.admin()
        self.enqueue()
        self.enqueue(2)
        self.bot.replies[900] = [(503, {'ok': False, 'description': 'private-panel-fixture'})]
        self.bot.replies[101] = [(403, {'ok': False, 'error_code': 403})]
        self.tick()
        self.tick(now=NOW + 1)
        self.assertEqual(self.bot.calls[0][1]['chat_id'], 900)
        self.assertEqual(self.status('admin')[0]['reason'], 'Telegram 503')
        self.assertEqual(self.status()[0]['reason'], 'Telegram 403')
        self.assertEqual(self.status()[1]['state'], 'sent')
        self.assertNotIn('private-panel-fixture', json.dumps(self.status('admin')))
        print('errors: admin=Telegram 503, client 1=Telegram 403, client 2=sent')

    def test_frozen_expired_missing_chat_and_conflicting_permissions(self):
        cases = [({'allowed': False}, 'клиент заморожен'),
                 ({'expires_at': STAMP}, 'срок доступа истёк'),
                 ({'telegram_chat_id': None}, 'нет канала доставки'),
                 ({'expires_at': 'broken'}, 'неизвестные или конфликтующие права')]
        for index, (changes, expected) in enumerate(cases):
            original = self.snapshot['clients'][0].copy()
            self.snapshot['clients'][0].update(changes)
            self.save_snapshot()
            self.enqueue(bundle_version=str(index))
            self.tick()
            self.assertEqual(self.status()[-1]['reason'], expected)
            self.snapshot['clients'][0] = original
        self.assertEqual(self.bot.calls, [])
        print('denied: frozen=0, expired=0, no Telegram=0; reasons visible in delivery_status')

    def test_access_and_device_ownership_rechecked_between_parts(self):
        self.enqueue()
        self.tick()
        self.snapshot['clients'][0]['expires_at'] = STAMP
        self.save_snapshot()
        self.tick()
        self.assertEqual(len(self.bot.calls), 1)
        self.assertEqual(self.status()[0]['reason'], 'срок доступа истёк')
        self.snapshot['clients'][0]['expires_at'] = None
        self.snapshot['clients'][0]['devices'] = []
        self.save_snapshot()
        self.tick()
        self.assertEqual(len(self.bot.calls), 1)
        self.assertEqual(self.status()[0]['reason'], 'состав устройств изменился')

    def test_snapshot_chat_change_does_not_leak_queued_profile(self):
        self.enqueue()
        self.snapshot['clients'][0]['telegram_chat_id'] = 777
        self.save_snapshot()
        self.tick()
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.status()[0]['reason'], 'канал доставки изменился')

    def test_no_token_is_visible_and_queue_recovers_when_token_returns(self):
        self.snapshot['bot_token'] = None
        self.save_snapshot()
        self.admin()
        self.enqueue()
        self.tick()
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.status()[0]['reason'], 'автодоставка невозможна: нет токена бота')
        self.assertEqual(self.status('admin')[0]['reason'], self.status()[0]['reason'])
        self.snapshot['bot_token'] = '456:replacement-fixture'
        self.save_snapshot()
        self.drain()
        self.assertEqual(len(self.bot.calls), 3)
        print('no token: автодоставка невозможна: нет токена бота; enqueue/run_once do not raise')

    def test_admin_ready_priority_retry_same_password_and_no_leak(self):
        self.enqueue()
        self.assertEqual(self.admin(panel_ready=False), {'admin': 'панель ещё не готова'})
        self.assertEqual(self.status('admin'), [])
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.admin()
            self.bot.replies[900] = [(500, {'ok': False, 'description': 'private-panel-fixture'})]
            self.tick()
            self.admin(panel_password='replacement-must-not-be-used')
            self.tick(now=NOW + 5)
        messages = [d['text'] for m, d in self.bot.calls if d['chat_id'] == 900]
        self.assertEqual(messages[0], messages[1])
        self.assertEqual(messages[0], 'Основной VPN недоступен. Аварийная панель запущена на exit-a. '
                         'Адрес: https://203.0.113.1. Пароль: private-panel-fixture. '
                         'Для входа нужен только пароль' + FOOTER)
        self.assertEqual(output.getvalue(), '')
        client_file = self.state / 'telegram/client.sqlite3'
        self.assertNotIn(b'private-panel-fixture', client_file.read_bytes())
        self.assertNotIn(b'123:fixture-only', client_file.read_bytes())
        self.assertEqual((self.state / 'telegram').stat().st_mode & 0o777, 0o700)
        self.assertEqual(client_file.stat().st_mode & 0o777, 0o600)
        print('admin: ready panel first; retry keeps password; client queue/logs contain no panel password')

    def test_self_signed_admin_message_warns_about_browser_and_gives_fingerprint(self):
        self.admin(certificate_fingerprint='AB12CD34')
        self.drain()
        text = self.bot.calls[0][1]['text']
        self.assertIn('Для входа нужен только пароль. Сертификат самоподписанный: браузер покажет '
                      'предупреждение о небезопасном подключении. Продолжайте, только если '
                      'отпечаток SHA-256 совпадает: AB12CD34' + FOOTER, text)
        print('admin: self-signed certificate -> browser warning sentence and SHA-256 fingerprint')

    def test_exact_leader_recovery_and_cancellation_texts(self):
        self.admin(event='leader_changed')
        self.enqueue(event='leader_changed')
        self.drain()
        self.assertEqual(self.bot.calls[0][1]['text'],
                         'Аварийный сервер сменился — новые адрес и пароль. Основной VPN '
                         'недоступен. Аварийная панель запущена на exit-a. Адрес: '
                         'https://203.0.113.1. Пароль: private-panel-fixture. Для входа нужен '
                         'только пароль' + FOOTER)
        expected = ('Аварийный сервер сменился. Для устройства телефон <&> 🦊 используйте новый '
                    'конфиг Hysteria2 ниже. Предыдущий аварийный профиль больше не используйте. '
                    'Как подключиться: https://203.0.113.1/instructions')
        self.assertEqual(self.bot.calls[1][1]['text'], expected + FOOTER)
        deadline = '2026-01-01T03:10:00+03:00'
        for event in ('recovered', 'return_cancelled'):
            self.admin(event=event, deadline=deadline)
            self.enqueue(event=event, deadline=deadline)
            self.drain()
        recovery = ('Основное VPN-подключение восстановлено. Отключите аварийное подключение '
                    'в используемом приложении и включите прежний профиль WireGuard, OpenVPN '
                    'или IKEv2. Аварийный доступ будет отключён в 03:10 01.01.2026 (UTC+03:00), '
                    'примерно через 10 минут')
        texts = [(data['chat_id'], data['text'].removesuffix(FOOTER)) for _, data in self.bot.calls]
        self.assertEqual(texts[3:], [
            (900, 'Основной VPN восстановлен. Аварийный режим будет завершён в 03:10 01.01.2026 '
                  '(UTC+03:00), примерно через 10 минут'),
            (101, recovery),
            (900, 'Основной VPN снова недоступен. Завершение аварийного режима отменено, '
                  'аварийная панель и доступ продолжают работать'),
            (101, 'Возврат отменён: основное VPN-подключение снова недоступно. Аварийный доступ '
                  'сохраняется — если вы уже переключились, снова включите аварийное '
                  'подключение Hysteria2')])
        self.assertTrue(all(data['text'].endswith(FOOTER) for _, data in self.bot.calls))
        print('texts: initial/leader/recovery verbatim from spec; deadline 03:10 01.01.2026 (UTC+03:00)')

    def test_long_unicode_name_and_long_profile_are_not_truncated(self):
        name = '🦊' * 4500 + '\n<&>'
        self.snapshot['clients'][0]['devices'][0]['name'] = name
        self.save_snapshot()
        uri = 'hysteria2://' + 'a' * 5000 + '@203.0.113.1:443/'
        self.profiles[11]['uri'] = uri
        self.enqueue()
        self.drain()
        messages = [d['text'] for m, d in self.bot.calls if m == 'sendMessage']
        footer = FOOTER
        joined = ''.join(t.removesuffix(footer) for t in messages)
        self.assertIn(name, joined)
        for text in messages:
            self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)
            self.assertTrue(text.endswith(footer))
        self.assertEqual(self.bot.calls[-1][0], 'sendDocument')
        self.assertEqual(self.bot.calls[-1][1]['document'], uri)
        self.assertEqual(self.bot.calls[-1][1]['caption'], footer.strip())

    def test_cancel_prevents_stale_delivery_and_prune_erases_history(self):
        self.admin()
        self.enqueue()
        tgqueue.cancel(self.state, emergency_id='outage-a', generation=1,
                       events=['initial', 'leader_changed'])
        self.enqueue()
        self.tick()
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.status()[0]['state'], 'cancelled')
        with closing(sqlite3.connect(self.state / 'telegram/admin.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT payload FROM part').fetchall(), [('',)])
        self.enqueue(emergency_id='outage-b')
        tgqueue.prune(self.state, keep_emergency_id='outage-b')
        self.assertEqual(len(self.status()), 1)
        self.assertEqual(self.status('admin'), [])

    def test_unknown_network_result_retries_without_secret_exception(self):
        self.enqueue()
        with patch.object(tgqueue.request.OpenerDirector, 'open',
                          side_effect=OSError('123:fixture-only private-panel-fixture')):
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.status()[0]['due'], NOW + 5)
        self.assertEqual(self.status()[0]['reason'], 'Telegram недоступен или ответ не подтверждён')
        self.assertEqual(self.tick(now=NOW + 5), 1)

    def test_api_redirect_is_not_followed(self):
        self.enqueue()
        self.bot.redirect_url = self.api + '/leak'
        self.bot.replies[101] = [(302, {'ok': False})]
        self.tick()
        self.assertEqual(len(self.bot.calls), 1)
        self.assertEqual(self.status()[0]['state'], 'retry')

    def test_worker_lock_does_not_block_or_duplicate(self):
        self.enqueue()
        with tgqueue._worker_lock(self.state):
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.tick(), 1)

    def test_truncated_http_response_is_retried(self):
        self.enqueue()
        with patch.object(tgqueue.request.OpenerDirector, 'open',
                          side_effect=tgqueue.HTTPException('truncated response')):
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.status()[0]['state'], 'retry')
        self.assertEqual(self.tick(now=NOW + 5), 1)

    def test_invalid_expiry_duplicates_and_missing_snapshot_fail_closed(self):
        self.snapshot['clients'].append(self.snapshot['clients'][0].copy())
        self.save_snapshot()
        self.enqueue()
        self.tick()
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.status()[0]['reason'], 'неизвестные или конфликтующие права')
        with patch.object(sys.modules['vpn2_emergency.snapshot'], 'load', return_value=None):
            self.tick()
            self.admin()
        self.assertEqual(self.bot.calls, [])

    def test_no_partial_bundle_on_missing_device_profile(self):
        self.snapshot['clients'][0]['devices'].append({'id': 33, 'name': 'extra'})
        self.save_snapshot()
        self.enqueue()
        self.drain()
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.status()[0]['reason'], 'нет аварийного профиля устройства')
        self.assertEqual(self.status()[0]['total'], 0)

    def test_enqueue_clients_records_every_client_with_reason(self):
        self.snapshot['clients'] += [
            {'id': 3, 'name': 'frozen', 'telegram_chat_id': 103, 'allowed': False,
             'expires_at': None, 'devices': [{'id': 33, 'name': 'x'}]},
            {'id': 4, 'name': 'expired', 'telegram_chat_id': 104, 'allowed': True,
             'expires_at': '2025-12-31T23:59:59+00:00', 'devices': [{'id': 44, 'name': 'x'}]},
            {'id': 5, 'name': 'no-telegram', 'telegram_chat_id': None, 'allowed': True,
             'expires_at': None, 'devices': [{'id': 55, 'name': 'x'}]}]
        self.save_snapshot()
        tgqueue.enqueue_clients(self.state, **(self.base | {
            'exit_url': 'https://203.0.113.1', 'profiles': self.profiles, 'now': NOW}))
        self.drain()
        rows = {row['client']: (row['state'], row['reason']) for row in self.status()}
        self.assertEqual(rows, {1: ('sent', ''), 2: ('sent', ''),
                                3: ('blocked', 'клиент заморожен'),
                                4: ('blocked', 'срок доступа истёк'),
                                5: ('blocked', 'нет канала доставки')})
        self.assertEqual({data['chat_id'] for _, data in self.bot.calls}, {101, 102})
        print('all clients:', rows)

    def test_worker_killed_while_request_in_flight(self):
        # systemctl restart mid-mailing: SIGKILL while Telegram has not answered.
        self.enqueue()
        self.enqueue(2)
        self.assertEqual(self.tick(), 2)
        self.bot.hold_call = 3
        worker = subprocess.Popen([sys.executable, __file__, '--worker', str(self.state), self.api])
        self.addCleanup(worker.wait)
        self.addCleanup(self.bot.release.set)
        self.assertTrue(self.bot.arrived.wait(10))
        worker.kill()
        worker.wait()
        self.bot.release.set()
        self.drain()
        sends = [(data['chat_id'], data['text'].removesuffix(FOOTER)[:22]) for _, data in self.bot.calls]
        confirmed, unconfirmed = sends[:2], sends[2]
        for item in confirmed:
            self.assertEqual(sends.count(item), 1)
        self.assertEqual(sends.count(unconfirmed), 2)
        self.assertEqual(len(sends), 5)
        self.assertTrue(all(row['state'] == 'sent' for row in self.status()))
        print(f'kill mid-send: confirmed resent=0, unconfirmed resent=1, calls={len(sends)}')

    def test_retry_uses_live_token_and_preserves_original_deadline(self):
        self.enqueue(event='recovered', deadline='2026-01-01T00:10:00Z')
        self.bot.replies[101] = [(200, {'ok': False, 'error_code': 429,
                                       'parameters': {'retry_after': 600}})]
        self.tick()
        self.snapshot['bot_token'] = '456:replacement-fixture'
        self.save_snapshot()
        with patch.object(tgqueue, '_send', wraps=tgqueue._send) as send:
            self.tick(now=NOW + 600)
            self.assertEqual(send.call_args.args[0], '456:replacement-fixture')
        self.assertEqual(self.bot.calls[0][1]['text'], self.bot.calls[1][1]['text'])


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        with snapshot_stub():
            tgqueue.run_once(sys.argv[2], api_base=sys.argv[3], now=NOW)
    else:
        unittest.main(verbosity=2)
