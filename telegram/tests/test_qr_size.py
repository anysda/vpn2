"""VPN2-598: QR from the bot must stay readable after Telegram recompresses the photo.

Telegram downsizes photos larger than 1280 px on a side, so the PNG has to fit
into 1280 px and still keep at least 8 px per module for the worst config.
The worst config is a real one issued by the panel: split tunnel with the
entry IP from 45.x cut out of AllowedIPs.

The panel draws the PNG, the bot only forwards it. fixtures/qr-worst-wg.png is
the panel's answer for qr-worst-wg.conf, recorded once; app/e2e/qr-size.sh
passes a fresh pair from a live panel in QR_WORST_CONF and QR_WORST_PNG.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zlib

import httpx

from harness import import_bot, update

HERE = Path(__file__).resolve().parent
LIVE = bool(os.environ.get('QR_WORST_PNG'))
WORST = Path(os.environ.get('QR_WORST_CONF') or HERE / 'qr-worst-wg.conf')
WORST_PNG = Path(os.environ.get('QR_WORST_PNG') or HERE / 'fixtures' / 'qr-worst-wg.png')
MAX_SIDE = 1280
MIN_PX = 8
BORDER = 4
# What the bot's own qrcode[pil] drew for qr-worst-wg.conf before the panel took over.
OLD_SIDE, OLD_PX, OLD_MODULES = 1197, 9, 125


def read_gray_png(png):
    """Rows of an 8-bit grayscale PNG without Pillow: the only kind the panel writes."""
    assert png[:8] == b'\x89PNG\r\n\x1a\n', 'not a PNG'
    pos, chunks = 8, {}
    while pos < len(png):
        size, kind = struct.unpack('>I4s', png[pos:pos + 8])
        chunks[kind] = chunks.get(kind, b'') + png[pos + 8:pos + 8 + size]
        pos += 12 + size
    width, height, depth, color, _, _, interlace = struct.unpack('>IIBBBBB', chunks[b'IHDR'])
    assert (depth, color, interlace) == (8, 0, 0), 'not 8-bit gray'
    raw = zlib.decompress(chunks[b'IDAT'])
    assert len(raw) == height * (width + 1)
    rows = []
    for y in range(height):
        line = raw[y * (width + 1):(y + 1) * (width + 1)]
        assert line[0] == 0, 'filtered row'
        rows.append(line[1:])
    return width, height, rows


def first_dark_run(rows):
    """Top-left finder's outer ring: where it starts and its seven dark modules in a row."""
    for y, row in enumerate(rows):
        for x, value in enumerate(row):
            if value < 128:
                run = 0
                while x + run < len(row) and row[x + run] < 128:
                    run += 1
                return x, y, run / 7
    raise AssertionError('QR without dark modules')


class QrSizeTests(unittest.TestCase):
    def test_worst_config_fits_telegram_and_keeps_8px_per_module(self):
        conf = WORST.read_text()
        width, height, rows = read_gray_png(WORST_PNG.read_bytes())
        x, y, px = first_dark_run(rows)
        modules = round(width / px) - 2 * BORDER
        print(f'\nQR панели для бота: конфиг {len(conf)} байт, версия {(modules - 17) // 4}, '
              f'модулей {modules}, PNG {width}x{height} px, {px:g} px/модуль')
        self.assertIn('AllowedIPs', conf)
        self.assertEqual(width, height)
        self.assertLessEqual(width, MAX_SIDE, 'PNG больше 1280 px: Telegram пережмёт фото')
        self.assertGreaterEqual(px, MIN_PX, 'меньше 8 px на модуль')
        self.assertEqual((x, y), (BORDER * px, BORDER * px), 'поле не 4 модуля')
        self.assertEqual(width, (modules + 2 * BORDER) * px)
        if not LIVE:
            self.assertEqual((width, px, modules), (OLD_SIDE, OLD_PX, OLD_MODULES),
                             'размер и поля не те, что рисовал бот')

    def test_png_decodes_to_the_exact_config(self):
        if not shutil.which('zbarimg'):
            # CI ставит zbar-tools: там пропуск спрятал бы непроверенный QR.
            if os.environ.get('CI'):
                self.fail('нет zbarimg (zbar-tools) в CI')
            self.skipTest('нет zbarimg (zbar-tools): декодер не из библиотеки бота')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'qr.png'
            path.write_bytes(WORST_PNG.read_bytes())
            text = subprocess.run(['zbarimg', '-q', '--raw', '-Sdisable', '-Sqrcode.enable', str(path)], check=True,
                                  capture_output=True, text=True).stdout
        self.assertEqual(text.removesuffix('\n'), WORST.read_text())


class QrFromPanelTests(unittest.IsolatedAsyncioTestCase):
    """The bot sends to Telegram exactly the bytes the panel answered with."""

    def setUp(self):
        self.bot = import_bot()
        self.addAsyncCleanup(self.bot.api.panel_http.aclose)
        self.addAsyncCleanup(self.bot.api.telegram_http.aclose)
        self.conf = WORST.read_text()
        self.png = WORST_PNG.read_bytes()
        self.panel_calls = []
        self.sent = []

    def telegram(self, request):
        self.sent.append((request.url.path.rsplit('/', 1)[-1], request.content))
        return httpx.Response(200, json={'ok': True, 'result': {}})

    def clients(self, panel):
        def handler(request):
            self.panel_calls.append(f'{request.method} {request.url.raw_path.decode()}')
            return panel(request)
        local = httpx.AsyncClient(base_url='http://panel.test', transport=httpx.MockTransport(handler))
        tg = httpx.AsyncClient(base_url='http://tg.test/bot', transport=httpx.MockTransport(self.telegram))
        self.addAsyncCleanup(local.aclose)
        self.addAsyncCleanup(tg.aclose)
        return patch.object(self.bot.api, 'panel_http', local), patch.object(self.bot.api, 'telegram_http', tg)

    def panel(self, png_status=200):
        def answer(request):
            path = request.url.path
            if path == '/api/bot/client':
                return httpx.Response(200, json={'devices': [{'id': 7, 'name': 'Телефон', 'configName': 'ivan-phone'}]})
            if path == '/api/bot/devices/7/wg-config':
                return httpx.Response(200, text=self.conf)
            if path == '/api/bot/devices/7/wg-qrcode.png':
                if png_status != 200:
                    return httpx.Response(png_status, json={'statusMessage': 'Server Error'})
                return httpx.Response(200, content=self.png, headers={'content-type': 'image/png'})
            return httpx.Response(404, json={'statusMessage': 'not_found'})
        return answer

    async def test_client_gets_the_panels_png_and_the_config(self):
        local, tg = self.clients(self.panel())
        with local, tg:
            await self.bot.handlers.dispatch(update('client', ('press', 'wg:7')), None)
        self.assertEqual(self.panel_calls, [
            'GET /api/bot/client?chatId=200',
            'GET /api/bot/devices/7/wg-config?chatId=200',
            'GET /api/bot/devices/7/wg-qrcode.png?chatId=200',
        ])
        self.assertEqual([method for method, _ in self.sent],
                         ['answerCallbackQuery', 'sendPhoto', 'sendDocument'])
        photo, document = self.sent[1][1], self.sent[2][1]
        self.assertIn(self.png, photo)
        self.assertIn('*Телефон* — WireGuard'.encode(), photo)
        self.assertIn(self.conf.encode(), document)
        self.assertIn(b'filename="ivan-phone.conf"', document)

    async def test_panel_failure_on_png_sends_nothing_but_the_usual_error(self):
        local, tg = self.clients(self.panel(png_status=502))
        with local, tg:
            await self.bot.handlers.dispatch(update('client', ('press', 'wg:7')), None)
        self.assertEqual([method for method, _ in self.sent], ['answerCallbackQuery', 'answerCallbackQuery'])
        self.assertEqual(json.loads(self.sent[1][1]), {
            'callback_query_id': 'callback', 'show_alert': True,
            'text': 'Панель временно недоступна. Попробуйте позже.'})

    async def send_event(self, extra):
        tasks = []
        request = SimpleNamespace(headers={'X-Tgbot-Secret': self.bot.config.SECRET}, json=AsyncMock(
            return_value={'type': 'client_send_wireguard', 'name': 'ivan-phone', 'conf': self.conf, **extra}))
        local, tg = self.clients(self.panel())
        with local, tg, patch.object(self.bot.events.asyncio, 'create_task', side_effect=tasks.append):
            self.assertEqual((await self.bot.events.handle_event(request)).status, 200)
            for task in tasks:
                await task
        self.assertEqual(self.panel_calls, [])
        return [method for method, _ in self.sent]

    async def test_admin_chat_gets_the_png_from_the_panels_event(self):
        methods = await self.send_event({'qr': base64.b64encode(self.png).decode()})
        self.assertEqual(methods, ['sendPhoto', 'sendDocument'])
        self.assertIn(self.png, self.sent[0][1])
        self.assertIn('*ivan-phone* — WireGuard'.encode(), self.sent[0][1])
        self.assertIn(self.conf.encode(), self.sent[1][1])

    async def test_event_without_png_still_delivers_the_config(self):
        for extra in ({}, {'qr': 'не base64'}, {'qr': base64.b64encode(b'not a png').decode()}, {'qr': 7}):
            with self.subTest(extra=extra):
                self.sent.clear()
                self.assertEqual(await self.send_event(extra), ['sendDocument'])


if __name__ == '__main__':
    unittest.main()
