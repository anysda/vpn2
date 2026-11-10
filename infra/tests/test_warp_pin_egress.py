"""Ручной маршрут на «-warp» выходит через WARP, а не direct-IP экзита (VPN2-650).

Прод 02-11: сторож хаба на минуту признаёт hy2-<узел>-warp мёртвым и переводит
pin-hy2-<узел>-warp на foreign-best, а там direct-ноги: «-warp» выходит с IP
дата-центра. Экзит при этом считает WARP живым. Запасной путь warp-маршрута
должен остаться на WARP либо держать маршрут на мёртвой ноге видно, но не
уводить его в direct молча.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from router_support import exit_cert
import watchdog_src


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
AA_DIRECT, AA_WARP = 'hy2-aa-direct', 'hy2-aa-warp'
BB_DIRECT, BB_WARP = 'hy2-bb-direct', 'hy2-bb-warp'


def router_config():
    spec = importlib.util.spec_from_file_location('router', ROOT / 'infra/vpn2/src/vpn2/router/config.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = {'EXIT_TAGS': 'aa bb', 'HY2_DIRECT_PORT': '443', 'HY2_WARP_PORT': '8443',
           'HY2_MGMT_PORT': '9443', 'WG_OUT_IFACE': 'lo', 'RU_CLASH_SECRET': 'test'}
    for n, tag in enumerate(('AA', 'BB'), start=2):
        env.update({f'DOMAIN_{tag}': '127.0.0.1', f'MGMT_IP_{tag}': f'192.0.2.{n}',
                    f'{tag}_HY2_CERT_PATH': exit_cert()})
        for key in ('DIRECT', 'WARP', 'MGMT', 'OBFS'):
            env[f'{tag}_{key}'] = 'test'
    output = io.StringIO()
    with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output):
        module.main()
    return json.loads(output.getvalue())


def watchdog():
    spec = importlib.util.spec_from_file_location('watchdog', watchdog_src.path('failover'))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {'DEAD_AFTER': '3'}, clear=True):
        spec.loader.exec_module(module)
    return module


def exit_legs(outbounds, tag, seen=()):
    """Ноги экзита (hy2-<узел>-direct|warp), до которых доходит группа."""
    out = outbounds[tag]
    if out['type'] not in ('selector', 'urltest'):
        return {tag}
    if tag in seen:
        return set()
    return set().union(*(exit_legs(outbounds, child, seen + (tag,)) for child in out['outbounds']))


class WarpPinRouterConfigTests(unittest.TestCase):
    def test_warp_pin_never_reaches_direct_leg(self):
        outbounds = {o['tag']: o for o in router_config()['outbounds']}
        for leg in (AA_WARP, BB_WARP):
            with self.subTest(pin=f'pin-{leg}'):
                legs = exit_legs(outbounds, f'pin-{leg}')
                direct = sorted(t for t in legs if not t.endswith('-warp'))
                self.assertEqual(direct, [], f'pin-{leg} доходит до direct-ног: «-warp» выйдет с IP экзита')

    def test_direct_pin_keeps_foreign_fallback(self):
        outbounds = {o['tag']: o for o in router_config()['outbounds']}
        self.assertEqual(outbounds[f'pin-{AA_DIRECT}']['outbounds'][0], AA_DIRECT)
        self.assertIn(BB_DIRECT, exit_legs(outbounds, f'pin-{AA_DIRECT}'))


class WarpPinWatchdogTests(unittest.TestCase):
    def dead_leg_puts(self, pin, alive):
        w = watchdog()
        proxies = {pin: {'type': 'Selector', 'now': pin[len('pin-'):]},
                   'foreign-best': {'type': 'Selector', 'now': BB_DIRECT}}
        with patch.object(w, '_tick_alive', alive), patch.object(w, '_req', return_value={}) as req, \
                contextlib.redirect_stdout(io.StringIO()):
            for _ in range(w.DEAD_AFTER + 1):
                w.pins_tick(proxies)
        return [c.args[2]['name'] for c in req.call_args_list
                if c.args[:2] == ('PUT', f'/proxies/{pin}')]

    def test_dead_warp_leg_does_not_move_pin_to_direct(self):
        # Сторож экзита WARP видит живым, хаб - три промаха подряд у aa-warp.
        targets = self.dead_leg_puts(f'pin-{AA_WARP}', {AA_DIRECT: 120, BB_DIRECT: 120, BB_WARP: 160})
        self.assertNotIn('foreign-best', targets,
                         'pin-hy2-aa-warp ушёл на foreign-best (сейчас hy2-bb-direct): «-warp» с direct-IP')
        self.assertTrue(all(t.endswith('-warp') or t == 'warp-best' for t in targets), targets)

    def test_dead_direct_leg_still_falls_back(self):
        targets = self.dead_leg_puts(f'pin-{AA_DIRECT}', {BB_DIRECT: 120, BB_WARP: 160})
        self.assertEqual(targets[-1:], ['foreign-best'])


class WarpExitProfileTests(unittest.TestCase):
    """Блок WARP 10-foreign.sh на фейковом wgcf: живой аккаунт, сбой generate."""

    def run_block(self, state, generate_fails=False):
        source = (ROOT / 'infra/scripts/10-foreign.sh').read_text()
        start = source.index('cd /etc/anysda\n')
        end = source.index('\n# ----', source.index('wgcf: placeholder', start))
        root = state.parents[1]
        wgcf = root / 'wgcf'
        wgcf.write_text('''#!/bin/sh
echo "$1" >> "$WGCF_CALLS"
case "$1" in
  generate)
    if [ "${WGCF_GENERATE_FAIL:-0}" = 1 ]; then
      echo 'failed to update bound device: Post "https://api.cloudflareclient.com": i/o timeout' >&2
      exit 1
    fi
    printf '[Interface]\\nPrivateKey = live-key\\nAddress = 172.16.0.2/32, fd00::2/128\\n[Peer]\\nPublicKey = peer-key\\nEndpoint = engage.cloudflareclient.com:2408\\n' > wgcf-profile.conf
    ;;
  register) exit 1 ;;
esac
''')
        wgcf.chmod(0o755)
        curl = root / 'curl'
        curl.write_text('#!/bin/sh\ncat >/dev/null\necho \'{"config": {"client_id": "AQID"}}\'\n')
        curl.chmod(0o755)
        calls = root / 'calls'
        calls.unlink(missing_ok=True)
        block = source[start:end].replace('/etc/anysda', str(state)).replace(
            '/usr/local/bin/wgcf', str(wgcf))
        script = ('HOST_TAG=test\n' + block +
                  '\necho "RESULT ok=$WGCF_OK key=$WGCF_PRIVKEY peer=$WGCF_PEER_ENDPOINT_HOST"\n')
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=state, text=True,
                                capture_output=True,
                                env=dict(os.environ, WGCF_CALLS=str(calls),
                                         WGCF_GENERATE_FAIL=str(int(generate_fails)),
                                         PATH=f'{root}:{os.environ["PATH"]}'))
        line = next((x for x in result.stdout.splitlines() if x.startswith('RESULT ')), '')
        return result, dict(kv.split('=', 1) for kv in line.split()[1:]), calls.read_text().split()

    def state_dir(self, temporary):
        state = Path(temporary) / 'etc/anysda'
        state.mkdir(parents=True)
        (state / 'wgcf-account.toml').write_text(
            'access_token = "token"\ndevice_id = "device"\nkey_type = "curve25519"\n')
        return state

    def test_generate_failure_with_live_account_is_not_silent_placeholder(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = self.state_dir(temporary)
            first, values, _ = self.run_block(state)
            self.assertEqual((first.returncode, values.get('ok')), (0, '1'), first.stderr)
            result, values, calls = self.run_block(state, generate_fails=True)
        self.assertEqual(calls, ['generate'])
        self.assertFalse(result.returncode == 0 and values.get('peer') == '127.0.0.1',
                         'рабочий профиль прошлого прогона заменён заглушкой 127.0.0.1: '
                         'WARP-нога мертва, сторож экзита снят, «-warp» уходит в direct')

    def test_redeploy_with_live_account_keeps_warp(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = self.state_dir(temporary)
            runs = [self.run_block(state) for _ in range(2)]
            account = (state / 'wgcf-account.toml').read_text()
        for result, values, calls in runs:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls, ['generate'])
            self.assertEqual(values, {'ok': '1', 'key': 'live-key', 'peer': 'engage.cloudflareclient.com'})
            self.assertNotIn('⚠', result.stdout)
        self.assertIn('device_id = "device"', account)


if __name__ == '__main__':
    unittest.main()
