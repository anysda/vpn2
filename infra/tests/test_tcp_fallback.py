"""Запасной канал entry → экзит по TCP (VLESS + Reality) рядом с Hysteria2.

Когда на пути режут UDP, все hy2-каналы экзита умирают, а TCP до него жив.
Каждый канал экзита (direct, warp, mgmt) на entry должен иметь резерв поверх
TCP, основной остаётся Hysteria2. Сторож при мёртвом hy2 и живом резерве не
перезапускает sing-box и шлёт одно событие на экзит; при мёртвых обоих -
перезапуск как раньше.

clash-api сторожа подменён моделью, собранной из настоящего конфига
генератора: selector идёт по `now`, outbound с detour - по detour,
hysteria2 жив при живом UDP до своего экзита, vless - при живом TCP.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
import socket

import router_support
import test_failover_restart as fr


ROOT = Path(__file__).resolve().parents[2]
SB = os.environ.get('SING_BOX_BIN') or shutil.which('sing-box')
KINDS = ('direct', 'warp', 'mgmt')
PASSWORDS = {'direct': 'test-direct-0123456789', 'warp': 'test-warp-0123456789',
             'mgmt': 'test-mgmt-0123456789', 'obfs': 'test-obfs-0123456789'}
SNI = 'www.example.com'
SID = '0123456789abcdef'
PORT = '8443'


def b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def reality_keypair():
    """Пара X25519 в форме Reality (base64url, 32 байта)."""
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / 'k.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'X25519', '-out', str(key)],
                       check=True, capture_output=True)
        priv = subprocess.run(['openssl', 'pkey', '-in', str(key), '-outform', 'DER'],
                              check=True, capture_output=True).stdout[-32:]
        pub = subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-outform', 'DER'],
                             check=True, capture_output=True).stdout[-32:]
    return b64url(priv), b64url(pub)


PRIV, PUB = reality_keypair()


def router_env(tags=('aa', 'bb')):
    env = {'EXIT_TAGS': ' '.join(tags)}
    for i, tag in enumerate(tags):
        T = tag.upper()
        env.update({f'DOMAIN_{T}': f'{tag}.example.test', f'MGMT_IP_{T}': f'192.0.2.{i + 2}',
                    f'{T}_DIRECT': PASSWORDS['direct'], f'{T}_WARP': PASSWORDS['warp'],
                    f'{T}_MGMT': PASSWORDS['mgmt'], f'{T}_OBFS': PASSWORDS['obfs'],
                    f'{T}_REALITY_PUBKEY': PUB, f'{T}_REALITY_SHORT_ID': SID,
                    f'{T}_REALITY_SNI': SNI, f'{T}_REALITY_PORT': PORT})
    return env


def router_config(tags=('aa', 'bb')):
    return json.loads(router_support.generate(**router_env(tags)))


def outbound_map(cfg):
    return {o['tag']: o for o in cfg['outbounds']}


def leaves(obs, tag, seen=()):
    """Все транспорты, до которых может дойти tag: группы и detour раскрываются."""
    if tag in seen or tag not in obs:
        return []
    o = obs[tag]
    seen = (*seen, tag)
    if o['type'] in ('selector', 'urltest'):
        return [leaf for member in o['outbounds'] for leaf in leaves(obs, member, seen)]
    if o.get('detour'):
        return leaves(obs, o['detour'], seen)
    return [o]


def is_reality(o):
    tls = o.get('tls') or {}
    return o['type'] == 'vless' and (tls.get('reality') or {}).get('enabled') is True


def node_of(o):
    return o['server'].split('.')[0]


def exit_config(env):
    """Конфиг экзита так, как его собирает 10-foreign: шаблон, envsubst и всё,
    что стадия делает с кандидатом до `sing-box check`."""
    source = (ROOT / 'infra/scripts/10-foreign.sh').read_text()
    block = source[source.index('TPL=/tmp/anysda/'):source.index('\n# Validate')]
    with tempfile.TemporaryDirectory() as tmp:
        stage, sb = Path(tmp) / 'anysda', Path(tmp) / 'sing-box'
        stage.mkdir()
        sb.mkdir()
        for path in (ROOT / 'infra/lib').iterdir():
            if path.is_file():
                shutil.copy(path, stage)
        shutil.copy(ROOT / 'infra/configs/sing-box-server.json.tpl', stage)
        block = block.replace('/tmp/anysda', str(stage)).replace('/etc/sing-box', str(sb))
        script = f'set -euo pipefail\nHOST_TAG=test\n{block}\ncat "$SB_CONFIG_TMP"\n'
        result = subprocess.run(['bash', '-c', script], text=True, capture_output=True,
                                env={**os.environ, **env})
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        return json.loads(result.stdout)


def exit_env():
    return {'HY2_DIRECT_PORT': '1443', 'HY2_WARP_PORT': '2443', 'HY2_MGMT_PORT': '3443',
            'HY2_PWD_DIRECT': PASSWORDS['direct'], 'HY2_PWD_WARP': PASSWORDS['warp'],
            'HY2_PWD_MGMT': PASSWORDS['mgmt'], 'HY2_OBFS_PWD': PASSWORDS['obfs'],
            'CLASH_SECRET': 'test', 'WGCF_PEER_ENDPOINT_HOST': '192.0.2.20',
            'WGCF_PEER_ENDPOINT_PORT': '2408', 'WGCF_LOCAL_IPV4': '192.0.2.21',
            'WGCF_RESERVED_JSON': '[0,0,0]',
            'WGCF_PRIVKEY': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
            'WGCF_PEER_PUBKEY': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
            'REALITY_PRIVKEY': PRIV, 'REALITY_SHORT_ID': SID, 'REALITY_SNI': SNI,
            'REALITY_PORT': PORT}


def sing_box_check(cfg, root):
    path = Path(root) / 'check.json'
    path.write_text(json.dumps(cfg))
    return subprocess.run([SB, 'check', '-c', str(path)], capture_output=True, text=True)


class TestRouterFallback(unittest.TestCase):
    def test_every_channel_of_every_exit_has_tcp_fallback(self):
        obs = outbound_map(router_config())
        for tag in ('aa', 'bb'):
            for kind in KINDS:
                channel = f'hy2-{tag}-{kind}'
                with self.subTest(channel=channel):
                    reach = leaves(obs, channel)
                    hy2 = [o for o in reach if o['type'] == 'hysteria2']
                    tcp = [o for o in reach if is_reality(o)]
                    self.assertTrue(hy2 and all(node_of(o) == tag for o in hy2),
                                    f'{channel}: нет Hysteria2 до своего экзита: {reach}')
                    self.assertTrue(tcp, f'{channel}: нет резерва VLESS + Reality, '
                                         f'достижимо только {[o["tag"] for o in reach]}')
                    self.assertTrue(all(node_of(o) == tag for o in tcp),
                                    f'{channel}: резерв ведёт на чужой экзит')
                    for o in tcp:
                        self.assertEqual(o['server_port'], int(PORT))
                        self.assertEqual(o['tls']['server_name'], SNI)
                        self.assertEqual(o['tls']['reality']['public_key'], PUB)
                        self.assertEqual(o['tls']['reality']['short_id'], SID)

    def test_hysteria2_is_primary(self):
        obs = outbound_map(router_config())
        groups = [o for o in obs.values() if o['type'] in ('selector', 'urltest')
                  and any(o2['type'] == 'hysteria2' for m in o['outbounds'] for o2 in leaves(obs, m))
                  and any(is_reality(o2) for m in o['outbounds'] for o2 in leaves(obs, m))]
        mixed = [g for g in groups
                 if len({node_of(leaf) for m in g['outbounds'] for leaf in leaves(obs, m)}) == 1]
        self.assertTrue(mixed, 'нет группы, где канал выбирает между hy2 и резервом')
        for g in mixed:
            with self.subTest(group=g['tag']):
                self.assertEqual(g['type'], 'selector',
                                 f'{g["tag"]}: выбор hy2/резерв - selector сторожа, не {g["type"]}')
                start = g.get('default') or g['outbounds'][0]
                self.assertTrue(all(o['type'] == 'hysteria2' for o in leaves(obs, start)),
                                f'{g["tag"]}: стартовый выбор {start} - не Hysteria2')

    def test_all_hy2_groups_see_fallback(self):
        obs = outbound_map(router_config())
        names = [t for t in obs if t in ('foreign-best', 'tg-best')
                 or t.startswith(('pin-', 'lane-'))]
        self.assertTrue(any(t.startswith('lane-') for t in names) and any(
            t.startswith('pin-') for t in names), names)
        for name in names:
            with self.subTest(group=name):
                for member in obs[name]['outbounds']:
                    if not member.startswith('hy2-'):
                        continue
                    reach = leaves(obs, member)
                    self.assertTrue(any(is_reality(o) for o in reach),
                                    f'{name}: канал {member} не видит резерв')

    def test_entry_and_exit_agree(self):
        obs = outbound_map(router_config(('aa',)))
        server = exit_config(exit_env())
        vless = [i for i in server['inbounds'] if i['type'] == 'vless']
        self.assertEqual(len(vless), 1, f'на экзите нет одного VLESS-инбаунда: {server["inbounds"]}')
        inbound = vless[0]
        reality = inbound['tls']['reality']
        self.assertTrue(reality['enabled'])
        self.assertEqual(reality['private_key'], PRIV)
        self.assertIn(SID, reality['short_id'])
        self.assertEqual(reality['handshake']['server'], SNI)
        self.assertEqual(inbound['listen_port'], int(PORT))
        users = {u['uuid'] for u in inbound['users']}
        for kind in KINDS:
            with self.subTest(kind=kind):
                tcp = [o for o in leaves(obs, f'hy2-aa-{kind}') if is_reality(o)]
                self.assertTrue(tcp)
                for o in tcp:
                    self.assertIn(o['uuid'], users, f'{kind}: uuid резерва entry экзит не знает')
        uuids = {kind: {o['uuid'] for o in leaves(obs, f'hy2-aa-{kind}') if is_reality(o)}
                 for kind in KINDS}
        self.assertEqual(len(set().union(*uuids.values())), 3,
                         f'у каналов должны быть разные пользователи VLESS: {uuids}')

    @unittest.skipUnless(SB, 'нужен sing-box')
    def test_configs_pass_sing_box_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = router_config()
            cfg['route'].pop('rule_set', None)
            cfg['route']['rules'] = [r for r in cfg['route']['rules'] if 'rule_set' not in json.dumps(r)]
            cfg['dns']['rules'] = [r for r in cfg['dns'].get('rules', []) if 'rule_set' not in json.dumps(r)]
            result = sing_box_check(cfg, tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            server = exit_config(exit_env())
            Path(tmp, 'tls.crt').write_text(Path(router_support.exit_cert()).read_text())
            text = json.dumps(server).replace('/etc/sing-box', tmp)
            Path(tmp, 'exit.json').write_text(text)
            result = subprocess.run([SB, 'check', '-c', str(Path(tmp, 'exit.json'))],
                                    capture_output=True, text=True)
            self.assertNotIn('vless', result.stderr.lower(), result.stderr)
            self.assertNotIn('reality', result.stderr.lower(), result.stderr)


class Clash:
    """clash-api sing-box по сгенерированному конфигу entry."""

    def __init__(self, cfg):
        self.obs = outbound_map(cfg)
        self.now = {t: o.get('default') or o['outbounds'][0] for t, o in self.obs.items()
                    if o['type'] in ('selector', 'urltest')}
        self.udp = {node_of(o) for o in self.obs.values() if o['type'] == 'hysteria2'}
        self.tcp = set(self.udp)
        self.puts = []

    def leaf(self, tag, depth=0):
        o = self.obs[tag]
        if depth > 10:
            raise RuntimeError(tag)
        if o['type'] in ('selector', 'urltest'):
            return self.leaf(self.now[tag], depth + 1)
        if o.get('detour'):
            return self.leaf(o['detour'], depth + 1)
        return o

    def alive(self, tag):
        o = self.leaf(tag)
        if o['type'] == 'hysteria2':
            return node_of(o) in self.udp
        if is_reality(o):
            return node_of(o) in self.tcp
        return True

    def proxy(self, name):
        o = self.obs[name]
        if o['type'] in ('selector', 'urltest'):
            return {'type': 'Selector' if o['type'] == 'selector' else 'URLTest',
                    'now': self.now[name], 'all': list(o['outbounds'])}
        return {'type': o['type'].capitalize()}

    def req(self, method, path, body=None, **_kw):
        path = urllib.parse.unquote(path.split('?')[0])
        name = path.split('/')[2] if path.startswith('/proxies/') else ''
        if method == 'PUT':
            if body['name'] not in self.obs[name]['outbounds']:
                raise urllib.error.HTTPError(path, 400, 'bad', None, None)
            self.now[name] = body['name']
            self.puts.append((name, body['name']))
            return {}
        if method == 'DELETE' or path == '/connections':
            return {'connections': []}
        if path.endswith('/delay'):
            if self.alive(name):
                return {'delay': 100}
            raise urllib.error.URLError(socket.timeout('timed out'))
        if path == '/proxies':
            return {'proxies': {n: self.proxy(n) for n in self.obs}}
        return self.proxy(name)


def udp_events(run):
    return [' '.join(map(str, args)) + ' ' + ' '.join(map(str, kw.values()))
            for args, kw in run.events if 'UDP' in ' '.join(map(str, (*args, *kw.values())))]


class TestWatchdogFallback(unittest.TestCase):
    def start(self, tmp, tags):
        w = fr.watchdog(tmp)
        clash = Clash(router_config(tags))
        return w, clash, fr.Run(w, clash, fr.Clock(w.INTERVAL))

    def test_udp_dead_tcp_alive_no_restart_one_event_and_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, run = self.start(tmp, ('aa',))
            clash.udp.discard('aa')
            run.go(30)
            log = run.out.getvalue()[-1500:]
            self.assertEqual(run.restarts, [], 'hy2 мёртв, резерв жив - sing-box '
                                               'перезапускать нельзя:\n' + log)
            events = udp_events(run)
            self.assertEqual(len(events), 1, f'ждём одно событие про UDP до aa: {run.events}\n{log}')
            self.assertIn('aa', events[0])
            self.assertIn('TCP', events[0])
            for kind in KINDS:
                self.assertTrue(is_reality(clash.leaf(f'hy2-aa-{kind}')),
                                f'{kind}: канал не ушёл на резерв\n{log}')
            clash.udp.add('aa')
            run.go(int(900 / w.INTERVAL))
            log = run.out.getvalue()[-1500:]
            events = udp_events(run)
            self.assertEqual(len(events), 2, f'ждём одно событие о возврате UDP: {run.events}\n{log}')
            self.assertIn('снова', events[1])
            for kind in KINDS:
                self.assertEqual(clash.leaf(f'hy2-aa-{kind}')['type'], 'hysteria2',
                                 f'{kind}: после возврата UDP канал не вернулся на hy2\n{log}')
            self.assertEqual(run.restarts, [])

    def test_return_waits_for_stable_udp(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, run = self.start(tmp, ('aa',))
            clash.udp.discard('aa')
            run.go(10)
            clash.udp.add('aa')
            run.go(2)
            self.assertTrue(is_reality(clash.leaf('hy2-aa-direct')),
                            'вернулся на hy2 сразу, без окна устойчивости')

    def test_lanes_stay_on_node_while_on_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, run = self.start(tmp, ('aa', 'bb'))
            before = {g: clash.now[g] for g in clash.now
                      if g in ('foreign-best', 'tg-best') or g.startswith(('lane-', 'pin-'))}
            clash.udp.discard('aa')
            run.go(40)
            moved = {g: (before[g], clash.now[g]) for g in before
                     if clash.now[g].split('-')[1] != before[g].split('-')[1]}
            self.assertEqual(moved, {}, 'при уходе aa на резерв группы перекинуты на другой узел')
            self.assertEqual(run.restarts, [])

    def test_udp_and_tcp_dead_restart_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, run = self.start(tmp, ('aa',))
            clash.udp.discard('aa')
            clash.tcp.discard('aa')
            run.go(13)
            self.assertEqual(run.restart_check, [3],
                             'мертвы и hy2, и резерв: перезапуск на 3-й проверке, '
                             f'было {run.restart_check}\n' + run.out.getvalue()[-1500:])


if __name__ == '__main__':
    unittest.main()
