import asyncio
import unittest

import httpx
from aiohttp import web

from support import load_bot


class ProxyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_credentials_never_reach_environment_proxy(self):
        panel_requests, proxy_requests = [], []

        async def panel(request):
            panel_requests.append(request.headers.get('Authorization'))
            return web.json_response({})

        async def proxy(request):
            proxy_requests.append((request.headers.get('Authorization'), await request.text()))
            return web.json_response({})

        ports = []
        for handler in (panel, proxy):
            app = web.Application()
            app.router.add_route('*', '/{path:.*}', handler)
            runner = web.AppRunner(app)
            await runner.setup()
            self.addAsyncCleanup(runner.cleanup)
            await web.TCPSite(runner, '127.0.0.1', 0).start()
            ports.append(runner.addresses[0][1])
        proxy_url = f'http://127.0.0.1:{ports[1]}'
        bot = load_bot({'ANYSDA_URL': f'http://127.0.0.1:{ports[0]}',
                        'HTTP_PROXY': proxy_url, 'HTTPS_PROXY': proxy_url,
                        'ALL_PROXY': proxy_url, 'NO_PROXY': ''})
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        await bot.api_status()
        await bot.api_link(200, 'TEST_ONLY_PASSWORD', 'test')
        self.assertEqual(len(proxy_requests), 0, 'panel requests leaked to environment proxy')
        self.assertEqual(panel_requests, [f'Bearer {bot.SECRET}'] * 2)

    async def test_telegram_sends_and_polling_use_explicit_tg_proxy(self):
        connects = []

        async def proxy(reader, writer):
            header = await reader.readuntil(b'\r\n\r\n')
            connects.append(header.split(b'\r\n')[0])
            writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(proxy, '127.0.0.1', 0)
        async def close_server():
            server.close()
            await server.wait_closed()
        self.addAsyncCleanup(close_server)
        proxy_url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        bot = load_bot({'TG_PROXY': proxy_url, 'HTTPS_PROXY': 'http://127.0.0.1:9'})
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        with self.assertRaises(httpx.ProxyError):
            await bot.tg_send(100, 'test')
        request = bot._WatchedRequest(proxy=bot.TG_PROXY)
        self.addAsyncCleanup(request.shutdown)
        with self.assertRaises(Exception):
            await request.do_request('https://api.telegram.org/test/getUpdates', 'POST')
        server.close()
        self.assertEqual(connects, [b'CONNECT api.telegram.org:443 HTTP/1.1'] * 2)


if __name__ == '__main__':
    unittest.main()
