"""wg0.conf outside apply: a `#` comment and blanks are dropped the way wg drops them.

wg (wireguard-tools config.c) cuts each line at the first `#`, removes every
blank and compares names in any ASCII case. So for wg `[Peer] # dev` opens a
peer and `PublicKey = k # note` is the key `k`. Each reader is cut out of its
script and run on forms of one wg0.conf that wg reads identically.
"""
import base64
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from wg_root import as_root

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))

SERVER = base64.b64encode(bytes(range(32))).decode()
FRESH = base64.b64encode(bytes(range(32, 64))).decode()
OTHER = base64.b64encode(bytes(range(64, 96))).decode()
PEER, OLD, NEW, GHOST = (base64.b64encode(bytes([n] * 32)).decode() for n in (201, 202, 203, 204))
PSK_ONE, PSK_TWO, PSK_NEW = (base64.b64encode(bytes([n] * 32)).decode() for n in (211, 212, 213))

# As 28-wireguard writes the header and backend appends the peers.
HEAD_LINE = '# Initial config — panel will overwrite via syncWireguardConfig().\n'


def conf(form):
    s = {'iface': '[Interface]', 'peer': '[Peer]', 'tail': '', 'eq': ' = ', 'sep': ', ', 'pre': '',
         'commented': False}
    s.update({
        'canonical': {},
        'section-comment': {'iface': '[Interface] # srv', 'peer': '[Peer] # dev'},
        'section-blank': {'iface': '[Interface] ', 'peer': '[Peer]\t'},
        'value-comment': {'tail': ' # note'},
        'commented-out': {'commented': True},
        'blanks': {'iface': '  [Interface]\t', 'peer': '\t[Peer]  ', 'tail': '#x', 'eq': '\t=  ', 'sep': ' ,\t',
                   'pre': ' \t'},
    }[form])
    t, eq, sep, pre = s['tail'], s['eq'], s['sep'], s['pre']

    def peer(key, psk, host):
        return (f'\n{s["peer"]}\n{pre}PublicKey{eq}{key}{t}\n{pre}PresharedKey{eq}{psk}{t}\n'
                f'{pre}AllowedIPs{eq}10.66.66.{host}/32{sep}fd66:66::{host}/128{t}\n')

    hidden = f'# [Peer]\n#PrivateKey = {OTHER}\n' if s['commented'] else ''
    ghost = f'\n# [Peer]\n#PublicKey = {GHOST}\n#AllowedIPs = 10.66.66.9/32\n' if s['commented'] else ''
    return (f'{HEAD_LINE}{s["iface"]}\n{pre}Address{eq}10.66.66.1/24{sep}fd66:66::1/64{t}\n'
            f'{pre}ListenPort{eq}51820{t}\n{hidden}{pre}PrivateKey{eq}{SERVER}{t}\n'
            + peer(PEER, PSK_ONE, 2) + peer(OLD, PSK_TWO, 3) + ghost)


def wg_view(text):
    """config.c config_read_line/process_line: [interface values, [peer values]] (:514, :528, :441-446)."""
    interface, peers, section = {}, [], None
    for raw in text.split('\n'):
        line = re.sub(r'[ \t\n\v\f\r]', '', raw.split('#', 1)[0])
        if not line:
            continue
        if line.lower() in ('[interface]', '[peer]'):
            section = line.lower()
            if section == '[peer]':
                peers.append({})
            continue
        name, _, value = line.partition('=')
        (interface if section == '[interface]' else peers[-1])[name.lower()] = value
    return [interface, peers]


def source(path):
    return (ROOT / path).read_text()


def cut(text, start, end):
    assert start in text, start
    body = text.split(start, 1)[1]
    assert end in body, end
    return start + body.split(end, 1)[0]


def bash(script, directory):
    env = dict(os.environ, LC_ALL='C.UTF-8')
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=directory, env=env,
                          text=True, capture_output=True, timeout=15)


# Stub of wg: pubkey of an empty key fails, genkey gives a key the host never had.
WG_STUB = ('wg() { if [[ $1 == genkey ]]; then echo ' + FRESH + '; return; fi\n'
           '  local key; key=$(cat); [[ -n $key ]] || return 1; echo "pub-$key"; }\n')


def load_verify():
    spec = importlib.util.spec_from_file_location('verify_traffic_comment', ROOT / 'infra/lib/verify-traffic.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VT = load_verify()


def verify_conf_peers(form):
    """verify-traffic.py conf_peers(): peers as restore_wg_apply and wg.apply take them."""
    return [(p['key'], p['preshared_key'], p['allowed']) for p in VT.conf_peers(conf(form))]


def verify_syncconf(form):
    """verify-traffic.py restore_wg_apply(): what wg reads from `wg syncconf wg0 /dev/stdin`."""
    sent = []
    saved = VT.WORK, VT.run, VT.restore_file
    with tempfile.TemporaryDirectory() as directory:
        VT.WORK = Path(directory)
        (VT.WORK / 'wg0.conf').write_text(conf(form))
        VT.run = lambda *args, data=None, **_kw: sent.append((args, data))
        VT.restore_file = lambda *_args: None
        try:
            VT.restore_wg_apply()
        finally:
            VT.WORK, VT.run, VT.restore_file = saved
    [(args, data)] = sent
    assert args[:3] == ('wg', 'syncconf', 'wg0'), args
    return wg_view(data)


def backup_check(form):
    """anysda-backup.sh --check-bundle: complaints about WireGuard peers."""
    block = cut(source('infra/lib/anysda-backup.sh'), "wg = root / 'etc-wireguard/wg0.conf'", '\nswan = root')
    rows = [(1, True, (PEER, PSK_ONE, '10.66.66.2')),
            (2, None, (NEW, PSK_NEW, '10.66.66.3'))]  # reissued: old key still on its address
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'etc-wireguard').mkdir()
        (root / 'etc-wireguard/wg0.conf').write_text(conf(form))
        scope = {'re': re, 'root': root, 'bad': [], 'retired_keys': set(), 'label': lambda d: f'устройство {d}',
                 'device_columns': {'wg_public_key', 'wg_preshared_key', 'wg_ip'},
                 'devices': lambda *_fields: iter(rows)}
        exec(block, scope)
    return scope['bad']


def restore_server_priv(form):
    """anysda-restore.sh: exit code and server.priv taken from [Interface] of the archived wg0.conf."""
    block = cut(source('infra/lib/anysda-restore.sh'), 'WG_BUNDLE="$BUNDLE_DIR/etc-wireguard"',
                '# Стадия 30 ждёт')
    with tempfile.TemporaryDirectory() as directory:
        bundle = Path(directory) / 'bundle/etc-wireguard'
        bundle.mkdir(parents=True)
        (bundle / 'wg0.conf').write_text(conf(form))
        result = bash('BUNDLE_DIR="$PWD/bundle"\n' + WG_STUB + block, directory)
        return result.returncode, (bundle / 'server.priv').read_text()


def svc_conf_iface(form):
    """anysda-svc.sh wg0_conf_iface: addresses wg0_apply compares with live wg0."""
    block = cut(source('infra/lib/anysda-svc.sh'), 'wg0_conf_iface() {', '\n}\n') + '\n}\n'
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'wg0.conf'
        path.write_text(conf(form))
        result = bash(block.replace('/etc/wireguard/wg0.conf', str(path)) + 'wg0_conf_iface\n', directory)
        return result.returncode, result.stdout.split('\n')[:-1]


def wireguard_rerun(form):
    """28-wireguard.sh wireguard_keys_build on a host that has only wg0.conf."""
    block = cut(source('infra/scripts/28-wireguard.sh'), 'wireguard_keys_build() {', '\n}\n') + '\n}\n'
    with tempfile.TemporaryDirectory() as directory:
        etc = Path(directory) / 'etc-wireguard'
        (etc / 'keys').mkdir(parents=True)
        (etc / 'wg0.conf').write_text(conf(form))
        result = bash(WG_STUB + block.replace('/etc/wireguard', str(etc)) +
                      f'wireguard_keys_build {etc}/keys\n', directory)
        priv = etc / 'keys/server.priv'
        return result.returncode, priv.read_text() if priv.exists() else None, result.stderr


PEERS = [(PEER, PSK_ONE, ['10.66.66.2/32', 'fd66:66::2/128']),
         (OLD, PSK_TWO, ['10.66.66.3/32', 'fd66:66::3/128'])]
SYNCCONF = [{'listenport': '51820', 'privatekey': SERVER},
            [{'publickey': PEER, 'presharedkey': PSK_ONE, 'allowedips': '10.66.66.2/32,fd66:66::2/128'},
             {'publickey': OLD, 'presharedkey': PSK_TWO, 'allowedips': '10.66.66.3/32,fd66:66::3/128'}]]
ADDRESSES = (0, ['addr 10.66.66.1/24', 'addr fd66:66::1/64'])


def every_reader(case, form):
    """The same answers as on the canonical file, from every reader."""
    with case.subTest('1a verify conf_peers', form=form):
        case.assertEqual(verify_conf_peers(form), PEERS)
    with case.subTest('1b verify syncconf body', form=form):
        case.assertEqual(verify_syncconf(form), SYNCCONF)
    with case.subTest('2 backup', form=form):
        case.assertEqual(backup_check(form), [])
    with case.subTest('3 restore server.priv', form=form):
        case.assertEqual(restore_server_priv(form), (0, SERVER + '\n'))
    with case.subTest('4 svc addresses', form=form):
        case.assertEqual(svc_conf_iface(form), ADDRESSES)
    with case.subTest('5 28-wireguard', form=form):
        code, priv, stderr = wireguard_rerun(form)
        case.assertEqual((code, priv), (0, SERVER + '\n'), stderr)


@as_root
class WgConfCommentTests(unittest.TestCase):
    """Edges 1-5 per reader, 6-7 comment lines and blanks, 8 the file as written."""

    def test_0_wg_reads_every_form_as_the_canonical_file(self):
        view = wg_view(conf('canonical'))
        self.assertEqual(view[0]['privatekey'], SERVER)
        self.assertEqual([p['publickey'] for p in view[1]], [PEER, OLD])
        for form in ('section-comment', 'section-blank', 'value-comment', 'commented-out', 'blanks'):
            with self.subTest(form):
                self.assertEqual(wg_view(conf(form)), view)

    def test_1_verify_keeps_peers_and_values(self):
        for form in ('section-comment', 'value-comment'):
            with self.subTest('conf_peers', form=form):
                self.assertEqual(verify_conf_peers(form), PEERS)
            with self.subTest('syncconf body', form=form):
                self.assertEqual(verify_syncconf(form), SYNCCONF)

    def test_2_backup_accepts_every_peer(self):
        for form in ('section-comment', 'value-comment'):
            with self.subTest(form):
                self.assertEqual(backup_check(form), [])

    def test_3_restore_takes_server_priv(self):
        for form in ('section-comment', 'value-comment'):
            with self.subTest(form):
                self.assertEqual(restore_server_priv(form), (0, SERVER + '\n'))

    def test_4_svc_addresses_match_live_wg0(self):
        for form in ('section-comment', 'section-blank', 'value-comment'):
            with self.subTest(form):
                self.assertEqual(svc_conf_iface(form), ADDRESSES)

    def test_5_wireguard_rerun_keeps_server_key(self):
        code, priv, stderr = wireguard_rerun('value-comment')
        self.assertEqual((code, priv), (0, SERVER + '\n'), stderr)

    def test_6_commented_out_lines_are_neither_section_nor_key(self):
        every_reader(self, 'commented-out')

    def test_7_blanks_and_comment_glued_to_value(self):
        every_reader(self, 'blanks')

    def test_8_file_as_written_reads_as_before(self):
        every_reader(self, 'canonical')


if __name__ == '__main__':
    unittest.main()
