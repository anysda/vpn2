"""wg0.conf with a latin-1 byte in a comment, which wg takes: control backend and vpn2-apply.

VPN2-616. After VPN2-595 the readers of wg0.conf take it as bytes, control still as text: at LANG=C or
C.UTF-8 Python decodes UTF-8, and `# caf\\xe9` was UnicodeDecodeError in
Backend.wireguard (wg.apply: add and remove a peer) and in Applier
install/rollback/mark wg. The panel could neither list nor change peers.

Each case runs in a child Python with LANG and LC_ALL set there (C starts UTF-8
mode), against the same wg0.conf with the comment as `cafe`: the outcome is the
same and the written files are the same bytes, with the comment byte kept.

1 backend wg.apply, latin-1 comment; 2 apply install/rollback/mark wg, the same;
3 the comment in ASCII and in UTF-8, written bytes as now;
4 other text files (sing-box config, server.conf and the CRL, settings json) as now.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
KEY = {name: name * 42 + 'A=' for name in 'abcs'}
PEER = {'a': {'key': KEY['a'], 'preshared_key': KEY['c'], 'ip': '10.66.66.2'},
        'b': {'key': KEY['b'], 'preshared_key': KEY['c'], 'ip': '10.66.66.3'}}
LOCALES = ('C', 'C.UTF-8')
LATIN1, ASCII, UTF8 = b'\xe9', b'e', b'\xc3\xa9'

PROBE = r'''
import importlib.util, json, sys, traceback
from pathlib import Path
root, case, work = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
peers = json.loads(sys.argv[4])
sys.path.insert(0, str(root / 'infra/control'))


def load(name):
    spec = importlib.util.spec_from_file_location(name, root / 'infra/control' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


synced = []
live = {}


def wg(*args, data=None):
    """wg and ip of the host: syncconf takes the peers, show reports them."""
    if args[1:2] == ('syncconf',):
        # As wg reads it: a line ends at #, the comment is not part of the config.
        synced.append('\n'.join(line.split('#', 1)[0] for line in data.split('\n')))
        live.clear()
        key = None
        for line in data.split('\n'):
            name, _, value = (part.strip() for part in line.partition('='))
            if name == 'PublicKey':
                key = value
                live[key] = {'ips': [], 'psk': ''}
            elif name == 'AllowedIPs':
                live[key]['ips'] = sorted(item.strip() for item in value.split(','))
            elif name == 'PresharedKey':
                live[key]['psk'] = value
        return ''
    if args[-1] == 'allowed-ips':
        return ''.join(f'{key}\t{" ".join(peer["ips"])}\n' for key, peer in live.items())
    if args[-1] == 'preshared-keys':
        return ''.join(f'{key}\t{peer["psk"]}\n' for key, peer in live.items())
    return ''


outcome = {'error': None, 'synced': synced, 'result': None}
try:
    if case.startswith('backend'):
        backend = load('backend')
        control = backend.Backend(work / 'root', work / 'state')
        control.pools = json.loads((root / 'app/shared/vpn-pools.json').read_bytes())
        control.command = wg
        if case == 'backend-add':
            outcome['result'] = control.execute('wg.apply', {'version': 1, 'peers': peers})
        elif case == 'backend-remove':
            outcome['result'] = control.execute('wg.apply', {'version': 1, 'peers': peers[:1]})
        elif case == 'backend-settings':
            outcome['result'] = control.execute('settings.read', {'resource': 'preferences'})
        elif case == 'backend-crl':
            def openssl(*args, data=None):
                if '-gencrl' in args:
                    Path(args[args.index('-out') + 1]).write_bytes((work / 'crl.out').read_bytes())
                return ''
            control.command = openssl
            control.deadline = float('inf')
            outcome['result'] = control.crl(control.path('/etc/openvpn/server'))
    else:
        apply = load('apply')
        def host(argv, data=None):
            if argv[:2] == ['systemctl', 'show']:
                return 'MainPID=100\nNRestarts=0\n'
            return wg(*argv, data=data)
        applier = apply.Applier(work / 'root', work / 'state', host, settle=0)
        resource = 'sing-box' if case == 'apply-sing-box' else 'wg'
        if case in ('apply-install', 'apply-sing-box'):
            applier.install(resource, work / 'new.conf')
        elif case == 'apply-rollback':
            applier.rollback('wg')
        elif case == 'apply-mark':
            applier.mark('wg')
except BaseException as error:
    outcome['error'] = f'{type(error).__name__}: {error}'
    outcome['code'] = getattr(error, 'code', None)
    outcome['trace'] = traceback.format_exc()[-1500:]
print(json.dumps(outcome))
'''


def conf(comment, *names, port=51820):
    """wg0.conf as deployed, the comment byte in a header line, after Address and PrivateKey and at the end."""
    body = (b'[Interface]\n# caf' + comment + b'\nAddress = 10.66.66.1/24 # caf' + comment +
            f'\nListenPort = {port}\nPrivateKey = {KEY["s"]} # caf'.encode() + comment + b'\n')
    for name in names:
        peer = PEER[name]
        suffix = peer['ip'].rsplit('.', 1)[1]
        body += (f'\n[Peer]\nPublicKey = {peer["key"]}\nPresharedKey = {peer["preshared_key"]}\n'
                 f'AllowedIPs = {peer["ip"]}/32, fd66:66::{suffix}/128\n').encode()
    # The card's step: printf '# caf\xe9\n' >> /etc/wireguard/wg0.conf
    return body + b'# caf' + comment + b'\n'


def tail(comment):
    return b'# caf' + comment + b'\n'


class Probe:
    """One case in a child Python at a locale; files in root/, state/ and beside."""

    def probe(self, case, locale, files, peers=('b', 'a')):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / 'state').mkdir()
            for name, body in files.items():
                path = work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
            env = {'PATH': os.environ['PATH'], 'LANG': locale, 'LC_ALL': locale}
            done = subprocess.run([sys.executable, '-I', '-c', PROBE, str(ROOT), case, str(work),
                                   json.dumps([PEER[name] for name in peers])],
                                  env=env, capture_output=True, timeout=60, check=False)
            self.assertEqual(done.returncode, 0, done.stderr.decode(errors='replace')[-2000:])
            outcome = json.loads(done.stdout)
            result = {name: (work / name).read_bytes() for name in files if (work / name).exists()}
            for name in ('root/etc/wireguard/wg0.conf', 'state/wg.good', 'state/sing-box.good',
                         'root/etc/openvpn/server/pki/crl.pem'):
                if (work / name).exists():
                    result[name] = (work / name).read_bytes()
            return outcome, result

    def same(self, case, comment, files, peers=('b', 'a')):
        """The case with the comment byte as the case with `cafe`: outcome and bytes."""
        for locale in LOCALES:
            with self.subTest(case=case, locale=locale, comment=comment):
                outcome, result = self.probe(case, locale, files(comment), peers)
                plain, expected = self.probe(case, locale, files(ASCII), peers)
                trace = outcome.pop('trace', None)
                plain.pop('trace', None)
                self.assertNotIn('UnicodeDecodeError', trace or '', trace)
                self.assertEqual(json.loads(json.dumps(outcome).replace('caf\\u00e9', 'cafe')), plain, trace)
                self.assertEqual(result, {name: body.replace(b'cafe', b'caf' + comment)
                                          for name, body in expected.items()})
                yield outcome, result

    def check_backend(self, comment):
        # Backend.wireguard keeps the header of wg0.conf and renders the peers after it.
        for outcome, result in self.same('backend-add', comment, backend_files('b')):
            self.assertIsNone(outcome['error'])
            self.assertEqual(sorted(outcome['result']['observed']['peers']), sorted([KEY['a'], KEY['b']]))
            self.assertEqual(result['root/etc/wireguard/wg0.conf'], conf(comment, 'b', 'a').removesuffix(tail(comment)))
        for outcome, result in self.same('backend-remove', comment, backend_files('b', 'a')):
            self.assertIsNone(outcome['error'])
            self.assertEqual(list(outcome['result']['observed']['peers']), [KEY['b']])
            self.assertEqual(result['root/etc/wireguard/wg0.conf'], conf(comment, 'b').removesuffix(tail(comment)))

    def check_apply(self, comment):
        # The header of the candidate or last good, the peers of wg0.conf as they are now.
        for outcome, result in self.same('apply-install', comment, install_files):
            self.assertIsNone(outcome['error'])
            self.assertEqual(result['root/etc/wireguard/wg0.conf'], conf(comment, 'b', port=51821))
            self.assertEqual(result['state/wg.good'], conf(comment, 'b', port=51821))
        for outcome, result in self.same('apply-rollback', comment, rollback_files):
            self.assertEqual(outcome['code'], 3, outcome['error'])
            self.assertEqual(result['root/etc/wireguard/wg0.conf'], conf(comment, 'b'))
            self.assertEqual(result['state/wg.good'], conf(comment, 'b'))
        for outcome, result in self.same('apply-mark', comment, mark_files):
            self.assertIsNone(outcome['error'])
            self.assertEqual(result['state/wg.good'], conf(comment, 'b'))


def backend_files(*names):
    return lambda comment: {'root/etc/wireguard/wg0.conf': conf(comment, *names)}


def install_files(comment):
    return {'root/etc/wireguard/wg0.conf': conf(comment, 'b'),
            'new.conf': conf(comment, port=51821).removesuffix(tail(comment))}


def rollback_files(comment):
    return {'root/etc/wireguard/wg0.conf': conf(comment, 'b', port=51821),
            'state/wg.good': conf(comment, 'a')}


def mark_files(comment):
    return {'root/etc/wireguard/wg0.conf': conf(comment, 'b')}


class BackendTests(Probe, unittest.TestCase):
    """1 wg.apply adds and removes a peer with `# caf\\xe9` in wg0.conf: the byte kept."""

    def test_latin1_comment(self):
        self.check_backend(LATIN1)


class ApplyTests(Probe, unittest.TestCase):
    """2 vpn2-apply install, rollback and mark wg with `# caf\\xe9`: the byte kept in wg0.conf and last good."""

    def test_latin1_comment(self):
        self.check_apply(LATIN1)


class AsNowTests(Probe, unittest.TestCase):
    """3 the comment in ASCII and in UTF-8: the same outcome and bytes as now."""

    def test_ascii_and_utf8_comment(self):
        for comment in (ASCII, UTF8):
            self.check_backend(comment)
            self.check_apply(comment)


class OtherFilesTests(Probe, unittest.TestCase):
    """4 the text files that are not wg0.conf: UTF-8 read and written as now."""

    def test_sing_box_config(self):
        def files(comment):
            body = b'{"log": {"level": "warn"}, "outbounds": [{"type": "direct", "tag": "caf' + comment + b'"}]}\n'
            return {'new.conf': body, 'root/etc/sing-box/config.json': body.replace(b'warn', b'info')}
        for outcome, result in self.same('apply-sing-box', UTF8, files):
            self.assertIsNone(outcome['error'])
            self.assertEqual(result['root/etc/sing-box/config.json'], files(UTF8)['new.conf'])
            self.assertEqual(result['state/sing-box.good'], files(UTF8)['new.conf'])

    def test_server_conf_and_crl(self):
        def files(comment):
            return {'root/etc/openvpn/server/server.conf': b'# caf' + comment +
                    b'\ncrl-verify /etc/openvpn/server/pki/crl.pem\n',
                    'root/etc/openvpn/server/pki/.keep': b'',
                    'crl.out': b'-----BEGIN X509 CRL-----\n# caf' + comment + b'\n-----END X509 CRL-----\n'}
        for outcome, result in self.same('backend-crl', UTF8, files):
            self.assertIsNone(outcome['error'])
            self.assertEqual(result['root/etc/openvpn/server/pki/crl.pem'], files(UTF8)['crl.out'])

    def test_settings_json(self):
        def files(comment):
            return {'root/etc/anysda/client-prefs.json': b'{"caf' + comment + b'": ["10.66.66.2"]}\n'}
        for outcome, _result in self.same('backend-settings', UTF8, files):
            self.assertEqual(outcome['result'], {'config': {'café': ['10.66.66.2']}})


if __name__ == '__main__':
    unittest.main()
