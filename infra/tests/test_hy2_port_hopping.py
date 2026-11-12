"""Смена портов hysteria2 у трёх каналов entry → экзит (VPN2-635).

Один постоянный UDP-поток до экзита фильтр провайдера режет по 5-tuple.
config.yaml `ports.hy2_hop` → переменные узла (all.env: HY2_HOP_ENABLED,
HY2_HOP_DIRECT, HY2_HOP_WARP, HY2_HOP_MGMT, HY2_HOP_INTERVAL) → outbound
entry получает диапазон и интервал, экзит перенаправляет диапазон на порт канала.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


from router_support import ROOT, generate, run_generator
from test_deploy import write_envs


CHANNELS = ('direct', 'warp', 'mgmt')
DEFAULTS = {'direct': (20000, 24999), 'warp': (25000, 29999), 'mgmt': (30000, 30999)}
LEGACY_KEYS = {'type', 'tag', 'server', 'server_port', 'password', 'obfs', 'tls',
               'up_mbps', 'down_mbps'}
HOP_KEYS = ('server_ports', 'hop_interval')

FAKE_NFT = '''#!/bin/sh
echo "nft $*" >> "$NFT_LOG"
prev=
for arg in "$@"; do
  if [ "$prev" = -f ]; then
    if [ "$arg" = - ]; then cat >> "$NFT_LOG"; else cat "$arg" >> "$NFT_LOG"; fi
  fi
  prev=$arg
done
'''
STUBS = '''
release_download() { printf '#!/bin/sh\\nexit 1\\n' > "$2"; }
sha256sum() {
  if [[ "${1:-}" == --check ]]; then cat > /dev/null; return 0; fi
  command sha256sum "$@"
}
'''


def candidates():
    sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))
    from vpn2.config import HY2_PORT_CANDIDATES
    return list(HY2_PORT_CANDIDATES)


def old_config():
    """config.yaml установки до смены портов: ключа ports.hy2_hop нет."""
    return (ROOT / 'config.example.yaml').read_text().replace('  passphrase: ""',
                                                              '  passphrase: test pass')


def with_hop(enabled='true', interval=None, **ranges):
    lines = ['ports:', '  hy2_hop:', f'    enabled: {enabled}']
    lines += [f'    {name}: {value}' for name, value in ranges.items()]
    if interval:
        lines.append(f'    interval: {interval}')
    return old_config().rstrip('\n') + '\n' + '\n'.join(lines) + '\n'


def convert(config, directory):
    (Path(directory) / 'config.yaml').write_text(config)
    return write_envs(directory, env={'VPN2_STATE_DIR': f'{directory}/state', 'HOME': directory})


def load_env(directory, host):
    """Переменные all.env + <host>.env так, как их загружает load_env деплоя."""
    envs = Path(directory) / 'infra/envs'
    dump = f'{sys.executable} -c "import json, os; print(json.dumps(dict(os.environ)))"'
    loaded = {}
    for files in ((), (envs / 'all.env', envs / f'{host}.env')):
        script = 'set -a; ' + ''.join(f'source "{f}"; ' for f in files) + dump
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                                env={'PATH': os.environ['PATH']}, check=True)
        loaded[bool(files)] = json.loads(result.stdout)
    return {k: v for k, v in loaded[True].items() if loaded[False].get(k) != v}


def hy2_outbounds(cfg):
    found = {}
    for outbound in cfg['outbounds']:
        match = re.fullmatch(r'(?:transport-)?hy2-(\w+)-(direct|warp|mgmt)', outbound['tag'])
        if outbound['type'] == 'hysteria2' and match:
            found[match.groups()] = outbound
    return found


class Hy2PortHoppingTests(unittest.TestCase):
    def entry(self, config):
        """config.yaml → переменные узла → генератор entry с env стадии 20-ru-router."""
        with tempfile.TemporaryDirectory() as directory:
            result = convert(config, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = load_env(directory, 'ru')
            all_env = (Path(directory) / 'infra/envs/all.env').read_text()
        overrides = dict(env)
        for tag in env['EXIT_TAGS'].split():
            for key in ('DIRECT', 'WARP', 'MGMT', 'OBFS'):
                overrides[f'{tag.upper()}_{key}'] = 'test-password-0123456789'
        result = run_generator(**overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        outbounds = hy2_outbounds(json.loads(result.stdout))
        self.assertEqual({tag for tag, _ in outbounds}, set(env['EXIT_TAGS'].split()))
        self.assertEqual(len(outbounds), 3 * len(env['EXIT_TAGS'].split()))
        return outbounds, env, all_env

    def assert_hopping(self, outbounds, env, ranges, interval):
        ports = {'warp': int(env['HY2_WARP_PORT']), 'mgmt': int(env['HY2_MGMT_PORT'])}
        bad = set(candidates())
        for tag in {tag for tag, _ in outbounds}:
            ports['direct'] = int(env.get(f'{tag.upper()}_HY2_DIRECT_PORT') or env['HY2_DIRECT_PORT'])
            spans = []
            for channel in CHANNELS:
                with self.subTest(exit=tag, channel=channel):
                    outbound = outbounds[tag, channel]
                    low, high = ranges[channel]
                    self.assertEqual(outbound.get('server_ports'), [f'{low}:{high}'])
                    self.assertEqual(outbound.get('hop_interval'), interval)
                    self.assertEqual(outbound['server_port'], ports[channel])
                    self.assertTrue(1 <= low <= high <= 32767, (low, high))
                    self.assertFalse({p for p in bad if low <= p <= high})
                    spans.append((low, high))
            spans.sort()
            for (_, high), (low, _) in zip(spans, spans[1:]):
                self.assertLess(high, low, f'{tag}: диапазоны каналов пересекаются: {spans}')

    # край 1
    def test_entry_outbounds_hop_on_all_three_channels(self):
        outbounds, env, _ = self.entry(with_hop('true'))
        self.assert_hopping(outbounds, env, DEFAULTS, '5s')

    # край 2: проверка на уровне env
    def test_disabled_hopping_keeps_outbounds_byte_identical(self):
        hop = {'HY2_HOP_DIRECT': '20000-24999', 'HY2_HOP_WARP': '25000-29999',
               'HY2_HOP_MGMT': '30000-30999', 'HY2_HOP_INTERVAL': '5s'}
        disabled = generate(HY2_HOP_ENABLED='0', **hop)
        enabled = json.loads(generate(HY2_HOP_ENABLED='1', **hop))
        for outbound in hy2_outbounds(json.loads(disabled)).values():
            self.assertLessEqual(set(outbound), LEGACY_KEYS, outbound['tag'])
        for outbound in enabled['outbounds']:
            for key in HOP_KEYS:
                outbound.pop(key, None)
        self.assertEqual(json.loads(disabled), enabled)

    # край 3
    def test_config_ranges_and_interval_reach_outbound_and_bad_ranges_fail(self):
        custom = {'direct': (21000, 21300), 'warp': (26000, 26999), 'mgmt': (31000, 31999)}
        outbounds, env, _ = self.entry(with_hop(
            'true', '45s', **{c: f'{a}-{b}' for c, (a, b) in custom.items()}))
        self.assert_hopping(outbounds, env, custom, '45s')

        outbounds, _, all_env = self.entry(with_hop('false'))
        self.assertIn('HY2_HOP_ENABLED=0\n', all_env)
        for outbound in outbounds.values():
            for key in HOP_KEYS:
                self.assertNotIn(key, outbound, outbound['tag'])

        defaults = {c: f'{a}-{b}' for c, (a, b) in DEFAULTS.items()}
        cases = [('пересечение direct/warp', {**defaults, 'warp': '24000-29999'}),
                 ('пересечение warp/mgmt', {**defaults, 'mgmt': '29000-30999'}),
                 ('выше 32767', {**defaults, 'mgmt': '30000-32768'})]
        cases += [(f'кандидат {p}', {**defaults, 'direct': f'{p}-{p + 1}'}) for p in candidates()]
        for name, ranges in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                result = convert(with_hop('true', **ranges), directory)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('ports.hy2_hop', result.stderr)

    # край 5 (сторона entry; сторона экзита - край 4 на том же конфиге)
    def test_old_config_without_hop_key_gets_defaults_enabled(self):
        outbounds, env, all_env = self.entry(old_config())
        self.assertNotIn('HY2_HOP_', all_env)
        self.assert_hopping(outbounds, env, DEFAULTS, '5s')


if __name__ == '__main__':
    unittest.main()
