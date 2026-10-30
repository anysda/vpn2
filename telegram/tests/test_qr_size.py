"""VPN2-598: QR from the bot must stay readable after Telegram recompresses the photo.

Telegram downsizes photos larger than 1280 px on a side, so the PNG has to fit
into 1280 px and still keep at least 8 px per module for the worst config.
The worst config is a real one issued by the panel: split tunnel with the
entry IP from 45.x cut out of AllowedIPs. app/e2e/qr-size.sh passes a fresh
one in QR_WORST_CONF; qr-worst-wg.conf is its copy for runs without a panel.
"""
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from PIL import Image

from support import load_bot

WORST = Path(os.environ.get('QR_WORST_CONF') or Path(__file__).with_name('qr-worst-wg.conf'))
MAX_SIDE = 1280
MIN_PX = 8


def module_px(image):
    """Module size from the outer ring of the top-left finder: seven dark modules in a row."""
    gray = image.convert('L')
    width, height = gray.size
    pixels = gray.load()
    for y in range(height):
        for x in range(width):
            if pixels[x, y] < 128:
                run = 0
                while x + run < width and pixels[x + run, y] < 128:
                    run += 1
                return run / 7
    raise AssertionError('QR without dark modules')


class QrSizeTests(unittest.IsolatedAsyncioTestCase):
    def render(self):
        bot = load_bot()
        self.addAsyncCleanup(bot._local.aclose)
        self.addAsyncCleanup(bot._tg.aclose)
        conf = WORST.read_text()
        return conf, bot._make_qr_png(conf)

    async def test_worst_config_fits_telegram_and_keeps_8px_per_module(self):
        conf, png = self.render()
        image = Image.open(io.BytesIO(png))
        px = module_px(image)
        modules = round(image.size[0] / px) - 8
        print(f'\nQR бота: конфиг {len(conf)} байт, версия {(modules - 17) // 4}, модулей {modules}, '
              f'PNG {image.size[0]}x{image.size[1]} px, {px:g} px/модуль')
        self.assertIn('AllowedIPs', conf)
        self.assertLessEqual(max(image.size), MAX_SIDE, 'PNG больше 1280 px: Telegram пережмёт фото')
        self.assertGreaterEqual(px, MIN_PX, 'меньше 8 px на модуль')

    async def test_png_decodes_to_the_exact_config(self):
        if not shutil.which('zbarimg'):
            # CI ставит zbar-tools: там пропуск спрятал бы непроверенный QR.
            if os.environ.get('CI'):
                self.fail('нет zbarimg (zbar-tools) в CI')
            self.skipTest('нет zbarimg (zbar-tools): декодер не из библиотеки бота')
        conf, png = self.render()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'qr.png'
            path.write_bytes(png)
            text = subprocess.run(['zbarimg', '-q', '--raw', str(path)], check=True,
                                  capture_output=True, text=True).stdout
        self.assertEqual(text.removesuffix('\n'), conf)


if __name__ == '__main__':
    unittest.main()
