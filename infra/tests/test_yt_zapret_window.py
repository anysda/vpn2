"""Регрессия VPN2-148: исполняем генерацию стратегии/nft без запуска стадии.

VPN2_TEST_ROOT позволяет прогнать те же проверки на исходном дереве.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
STAGE = (ROOT / 'infra/scripts/19-yt-zapret.sh').read_text()
DEFAULTS = STAGE[STAGE.index('YT_ROUTE='):STAGE.index("DIAG_USER=")]
WINDOW = STAGE[STAGE.index("WIN_SCALE=''"):STAGE.index('cat > "$NFT_CONF"')]
SPLIT = '--payload=tls_client_hello --lua-desync=multisplit:pos=10,midsld:seqovl=1'


def render(**settings):
    env = {k: v for k, v in os.environ.items() if not k.startswith('YT_')}
    env.update(HOST_TAG='ru', WAN_IFACE='eth0', YT_ZAPRET_WSCALE='9')
    env.update(settings)
    return subprocess.run(
        ['bash', '-euc', DEFAULTS + '\n' + WINDOW
         + '\nprintf "STRATEGY=%s\\nNFT=%s\\n" "$YT_ZAPRET_STRATEGY" "$NFT_WIN_RULES"'],
        env=env, text=True, capture_output=True, check=False,
    )


class WindowTests(unittest.TestCase):
    def test_compat_default(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('STRATEGY=--lua-desync=wssize:wsize=1:scale=1 ' + SPLIT, result.stdout)
        self.assertIn('tcp window > 255 counter tcp window set 65535', result.stdout)
        self.assertIn('tcp window << 8', result.stdout)

    def test_native_preserves_kernel_scale_and_split(self):
        result = render(YT_ZAPRET_WINDOW_SCALE='native')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('STRATEGY=--lua-desync=wssize:wsize=1 ' + SPLIT, result.stdout)
        self.assertTrue(result.stdout.endswith('NFT=\n'), result.stdout)

    def test_scale_eight_compensates_one_bit(self):
        result = render(YT_ZAPRET_WINDOW_SCALE='8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('wssize:wsize=1:scale=8 ' + SPLIT, result.stdout)
        self.assertIn('tcp window > 32767 counter tcp window set 65535', result.stdout)
        self.assertIn('tcp window << 1', result.stdout)

    def test_scale_nine_needs_no_compensation(self):
        result = render(YT_ZAPRET_WINDOW_SCALE='9')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('wssize:wsize=1:scale=9 ' + SPLIT, result.stdout)
        self.assertTrue(result.stdout.endswith('NFT=\n'), result.stdout)

    def test_requested_scale_above_kernel_does_not_shrink_window(self):
        result = render(YT_ZAPRET_WSCALE='6', YT_ZAPRET_STRATEGY='--lua-desync=wssize:wsize=1:scale=9 ' + SPLIT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith('NFT=\n'), result.stdout)

    def test_custom_strategy_wins(self):
        strategy = '--lua-desync=wssize:wsize=1:scale=1 ' + SPLIT
        result = render(YT_ZAPRET_WINDOW_SCALE='native', YT_ZAPRET_STRATEGY=strategy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('STRATEGY=' + strategy, result.stdout)
        self.assertIn('tcp window << 8', result.stdout)
        result = render(YT_ZAPRET_STRATEGY=SPLIT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith('NFT=\n'), result.stdout)

    def test_invalid_scale_rejected_by_stage(self):
        for scale in ('15', '-1', '09', 'auto', '1 --new'):
            with self.subTest(scale=scale):
                result = render(YT_ZAPRET_WINDOW_SCALE=scale)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('YT_ZAPRET_WINDOW_SCALE', result.stderr)

    def test_config_to_env(self):
        for scale in ('native', '0', '1', '8', '9', '14', '15', '-1', '09', 'auto'):
            with self.subTest(scale=scale), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = (ROOT / 'config.example.yaml').read_text()
                (root / 'config.yaml').write_text(config + f'\nyoutube:\n  route: zapret\n  window_scale: {scale}\n')
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'infra/lib/config2env.py'), directory],
                    text=True, capture_output=True, check=False,
                )
                if scale in ('15', '-1', '09', 'auto'):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('youtube.window_scale', result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"YT_ZAPRET_WINDOW_SCALE='{scale}'", (root / 'infra/envs/ru.env').read_text())


if __name__ == '__main__':
    unittest.main()
