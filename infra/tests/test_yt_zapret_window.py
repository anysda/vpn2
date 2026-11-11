"""Регрессия VPN2-148: стратегия и поправка окна nft из настроек, без запуска роли.

VPN2_TEST_ROOT позволяет прогнать те же проверки на исходном дереве.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from zapret_support import stage


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
SPLIT = '--payload=tls_client_hello --lua-desync=multisplit:pos=10,midsld:seqovl=1'


def render(**settings):
    """(стратегия, правила nft) при масштабе окна ядра 9."""
    cfg = stage.settings({'YT_ZAPRET_WSCALE': '9', **settings})
    return cfg['strategy'], stage.window_rules(cfg, 'eth0')


class WindowTests(unittest.TestCase):
    def test_compat_default(self):
        strategy, rules = render()
        self.assertEqual(strategy, '--lua-desync=wssize:wsize=1:scale=1 ' + SPLIT)
        self.assertIn('tcp window > 255 counter tcp window set 65535', rules)
        self.assertIn('tcp window << 8', rules)
        self.assertTrue(all(line.startswith('        meta mark 256 oifname "eth0" tcp dport 443 ')
                            for line in rules.splitlines()))

    def test_native_preserves_kernel_scale_and_split(self):
        self.assertEqual(render(YT_ZAPRET_WINDOW_SCALE='native'), ('--lua-desync=wssize:wsize=1 ' + SPLIT, ''))

    def test_scale_eight_compensates_one_bit(self):
        strategy, rules = render(YT_ZAPRET_WINDOW_SCALE='8')
        self.assertEqual(strategy, '--lua-desync=wssize:wsize=1:scale=8 ' + SPLIT)
        self.assertIn('tcp window > 32767 counter tcp window set 65535', rules)
        self.assertIn('tcp window << 1', rules)

    def test_scale_nine_needs_no_compensation(self):
        self.assertEqual(render(YT_ZAPRET_WINDOW_SCALE='9'), ('--lua-desync=wssize:wsize=1:scale=9 ' + SPLIT, ''))

    def test_requested_scale_above_kernel_does_not_shrink_window(self):
        _, rules = render(YT_ZAPRET_WSCALE='6', YT_ZAPRET_STRATEGY='--lua-desync=wssize:wsize=1:scale=9 ' + SPLIT)
        self.assertEqual(rules, '')

    def test_kernel_scale_is_measured_when_not_given(self):
        cfg = stage.settings({})
        self.assertIn('tcp window << 6', stage.window_rules(cfg, 'eth0', measure=lambda: '7'))
        # Замер не удался - берётся масштаб прода.
        self.assertIn('tcp window << 8', stage.window_rules(cfg, 'eth0', measure=lambda: ''))

    def test_custom_strategy_wins(self):
        custom = '--lua-desync=wssize:wsize=1:scale=1 ' + SPLIT
        strategy, rules = render(YT_ZAPRET_WINDOW_SCALE='native', YT_ZAPRET_STRATEGY=custom)
        self.assertEqual(strategy, custom)
        self.assertIn('tcp window << 8', rules)
        self.assertEqual(render(YT_ZAPRET_STRATEGY=SPLIT), (SPLIT, ''))

    def test_invalid_scale_rejected_by_stage(self):
        for scale in ('15', '-1', '09', 'auto', '1 --new'):
            with self.subTest(scale=scale), self.assertRaisesRegex(stage.StageError, 'YT_ZAPRET_WINDOW_SCALE'):
                render(YT_ZAPRET_WINDOW_SCALE=scale)

    def test_empty_values_fall_back_to_defaults(self):
        cfg = stage.settings({'YT_ROUTE': '', 'YT_QUIC': '', 'YT_MARK': '', 'YT_ZAPRET_BASE': ''})
        self.assertEqual((cfg['route'], cfg['quic'], cfg['mark'], cfg['bin']),
                         ('off', 'block', '256', '/opt/zapret2/binaries/my/nfqws2'))
        with self.assertRaisesRegex(stage.StageError, 'YT_QUIC'):
            stage.settings({'YT_QUIC': 'maybe'})

    def test_config_to_env(self):
        for scale in ('native', '0', '1', '8', '9', '14', '15', '-1', '09', 'auto'):
            with self.subTest(scale=scale), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = (ROOT / 'config.example.yaml').read_text().replace(
                    '  passphrase: ""', '  passphrase: test pass')
                (root / 'config.yaml').write_text(config + f'\nyoutube:\n  route: zapret\n  window_scale: {scale}\n')
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'infra/lib/config2env.py'), directory],
                    text=True, capture_output=True, check=False,
                                        env={**os.environ, 'VPN2_STATE_DIR': f'{directory}/state'})
                if scale in ('15', '-1', '09', 'auto'):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('youtube.window_scale', result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f'YT_ZAPRET_WINDOW_SCALE=' + (scale if scale.isdigit() else f"'{scale}'"), (root / 'infra/envs/ru.env').read_text())


if __name__ == '__main__':
    unittest.main()
