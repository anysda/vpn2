"""Real bot processes, HTTPS Bot API and CONNECT proxy; no real credentials.

BOT_REVISION=lane/t (or origin/dev) runs the same recovery assertions against
the old bot. Those runs must fail: HTTP 200 does not imply durable acceptance.
"""
import asyncio
from contextlib import suppress
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

import httpx
from aiohttp import web


CHILD = '''
import asyncio, os
from support import load_bot
bot = load_bot({key: os.environ[key] for key in (
    'TG_PROXY', 'TGBOT_EVENT_PORT', 'TGBOT_OUTBOX_PATH', 'SSL_CERT_FILE',
    'TGBOT_POLL_STALL_SEC', 'ANYSDA_URL')})
asyncio.run(bot.main())
'''


class DeliveryProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):  # noqa - unittest lifecycle hook, called by the framework
        self.directory = tempfile.TemporaryDirectory(prefix='tgbot-delivery-')
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        cert, key = root / 'cert.pem', root / 'key.pem'
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=api.telegram.org', '-addext', 'subjectAltName=DNS:api.telegram.org',
            '-keyout', str(key), '-out', str(cert),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        self.available = True
        self.messages = []
        self.tunnels = set()
        self.proxy_tasks = set()
        self.connects = 0
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', self.telegram)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        await web.TCPSite(runner, '127.0.0.1', 0, ssl_context=tls).start()
        self.api_port = runner.addresses[0][1]
        self.proxy_server = await asyncio.start_server(self.proxy, '127.0.0.1', 0)
        self.addAsyncCleanup(self.close_proxy)
        proxy_port = self.proxy_server.sockets[0].getsockname()[1]
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        self.env = {
            **os.environ, 'PYTHONPATH': str(Path(__file__).parent),
            'TG_PROXY': f'http://127.0.0.1:{proxy_port}', 'TGBOT_EVENT_PORT': str(port),
            'TGBOT_OUTBOX_PATH': str(root / 'outbox.sqlite'), 'SSL_CERT_FILE': str(cert),
            'TGBOT_POLL_STALL_SEC': '0', 'ANYSDA_URL': 'http://127.0.0.1:9',
        }
        self.process = None
        self.log_path = root / 'bot.log'
        self.log_file = self.log_path.open('w')
        self.addCleanup(self.log_file.close)
        self.client = httpx.AsyncClient(base_url=self.url, trust_env=False, timeout=1.5)
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.stop_bot)

    async def telegram(self, request):
        method = request.path.rsplit('/', 1)[-1]
        if method == 'getMe':
            result = {'id': 123456, 'is_bot': True, 'first_name': 'test', 'username': 'test_bot'}
        elif method == 'getUpdates':
            await asyncio.sleep(0.1)
            result = []
        elif method == 'sendMessage':
            payload = await request.json()
            self.messages.append(payload)
            result = {'message_id': len(self.messages)}
        else:
            result = True
        response = web.json_response({'ok': True, 'result': result})
        response.force_close()
        return response

    async def proxy(self, reader, writer):
        task = asyncio.current_task()
        self.proxy_tasks.add(task)
        self.tunnels.add(writer)
        remote = None
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            self.assertTrue(header.startswith(b'CONNECT api.telegram.org:443 '))
            self.connects += 1
            if not self.available:
                writer.write(b'HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
                return
            upstream, remote = await asyncio.open_connection('127.0.0.1', self.api_port)
            writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            await writer.drain()

            async def copy(source, target):
                try:
                    while chunk := await source.read(65536):
                        target.write(chunk)
                        await target.drain()
                finally:
                    target.close()

            await asyncio.gather(copy(reader, remote), copy(upstream, writer))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if remote:
                remote.close()
            writer.close()
            self.tunnels.discard(writer)
            self.proxy_tasks.discard(task)

    async def close_proxy(self):
        self.proxy_server.close()
        await self.proxy_server.wait_closed()
        for writer in self.tunnels:
            writer.close()
        await asyncio.gather(*self.proxy_tasks, return_exceptions=True)

    async def start_bot(self):
        log_offset = self.log_path.stat().st_size
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, '-u', '-c', CHILD, env=self.env,
            stdout=self.log_file, stderr=subprocess.STDOUT)
        async with asyncio.timeout(30):
            while True:
                if self.process.returncode is not None:
                    self.fail(self.log_path.read_text())
                try:
                    await self.client.get('/health')
                    if not self.available or 'Bot started' in self.log_path.read_text()[log_offset:]:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)

    async def stop_bot(self):
        if self.process and self.process.returncode is None:
            self.process.kill()  # SIGKILL: no graceful flush can rescue the test.
            await self.process.wait()

    async def scenario(self, restart):
        await self.start_bot()
        self.available = False
        for writer in self.tunnels:
            writer.close()
        payloads = [
            {'type': 'system_alert', 'subject': 'outage-regression', 'detail': 'durable alert'},
            {'type': 'client_notify', 'chatId': 200, 'text': 'durable client notification'},
        ]
        latencies = []
        for payload in payloads:
            start = time.monotonic()
            response = await self.client.post('/event', json=payload,
                                              headers={'X-Tgbot-Secret': 'TEST_ONLY_EVENT_SECRET'})
            latencies.append(round((time.monotonic() - start) * 1000, 1))
            self.assertEqual(response.status_code, 200)
        # SIGKILL during claim/send leaves a deliberate 120s lease. Wait for
        # both failed sends to persist their retry state before this outage
        # recovery scenario kills the process (lease recovery has unit coverage).
        async with asyncio.timeout(30):
            while self.log_path.read_text().count('outbox retry id=') < 2:
                self.assertIsNone(self.process.returncode, self.log_path.read_text())
                await asyncio.sleep(0.05)
        self.assertEqual(self.messages, [])
        if restart:
            await self.stop_bot()
        self.available = True
        if restart:
            await self.start_bot()
        with suppress(TimeoutError):
            async with asyncio.timeout(70):
                while len(self.messages) < 2:
                    await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
        print(f'revision={os.environ.get("BOT_REVISION", "working-tree")} restart={restart} '
              f'ack_ms={latencies} delivered={len(self.messages)} expected=2', flush=True)
        self.assertEqual(len(self.messages), 2, self.log_path.read_text()[-3000:])
        self.assertEqual([message['chat_id'] for message in self.messages], [100, 200])
        self.assertIn('outage-regression', self.messages[0]['text'])
        self.assertEqual(self.messages[1]['text'], payloads[1]['text'])
        self.assertGreater(self.connects, 0)

    async def test_outage_recovers_without_restart(self):
        await self.scenario(False)

    async def test_outage_recovers_after_process_kill(self):
        await self.scenario(True)

    @unittest.skipIf(os.environ.get('BOT_REVISION'), 'new startup behavior')
    async def test_accepts_events_when_started_offline(self):
        self.available = False
        await self.start_bot()
        response = await self.client.post('/event', json={
            'type': 'system_alert', 'subject': 'offline startup',
        }, headers={'X-Tgbot-Secret': 'TEST_ONLY_EVENT_SECRET'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Path(self.env['TGBOT_OUTBOX_PATH']).is_file())


if __name__ == '__main__':
    unittest.main()
