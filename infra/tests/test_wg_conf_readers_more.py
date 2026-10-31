"""wg0.conf outside apply: more forms that wg and wg-quick read one way.

restore.sh edits and reads wg0.conf in place (server key, live peers, revoked
peers). wg (wireguard-tools config.c) adds up every AllowedIPs line of a peer,
and wg-quick splits Address at commas and blanks alike. Each reader is cut out
of its script and run on forms of one wg0.conf.
"""
import base64
import hashlib
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
PEER = base64.b64encode(bytes(range(64, 96))).decode()
GONE = base64.b64encode(bytes(range(96, 128))).decode()
OLD = base64.b64encode(bytes(range(128, 160))).decode()
NEW = base64.b64encode(bytes(range(160, 192))).decode()
PSK_ONE, PSK_TWO, PSK_NEW, PSK_GONE = (base64.b64encode(bytes([n] * 32)).decode() for n in (211, 212, 213, 214))

# As 28-wireguard writes the header and backend appends the peers.
HEAD_LINE = '# Initial config — panel will overwrite via syncWireguardConfig().\n'
ADDRESS = {'address-space': ['10.66.66.1/24 fd66:66::1/64'],
           'address-two-lines': ['10.66.66.1/24', 'fd66:66::1/64']}
EDIT = {
    'value-comment': lambda line: line + '#c' if line.startswith(('PrivateKey', 'PublicKey')) else line,
    'blank-in-name': lambda line: line.replace('PrivateKey', 'Private\tKey').replace('PublicKey', 'Public\tKey'),
    'section-comment': lambda line: line + ' # x' if line in ('[Interface]', '[Peer]') else line,
}


def parts(form):
    """Header and one block per peer; the file is their concatenation."""
    address = ADDRESS.get(form, ['10.66.66.1/24, fd66:66::1/64'])
    head = (HEAD_LINE + '[Interface]\n' + ''.join(f'Address = {a}\n' for a in address) +
            f'ListenPort = 51820\nPrivateKey = {SERVER}\n')
    peers = []
    for key, psk, host in ((PEER, PSK_ONE, 2), (GONE, PSK_GONE, 4), (OLD, PSK_TWO, 3)):
        allowed = [f'10.66.66.{host}/32', f'fd66:66::{host}/128']
        lines = allowed if form == 'allowed-two-lines' and key == OLD else [', '.join(allowed)]
        peers.append(f'\n[Peer]\nPublicKey = {key}\nPresharedKey = {psk}\n' +
                     ''.join(f'AllowedIPs = {a}\n' for a in lines))
    edit = EDIT.get(form, lambda line: line)
    return ['\n'.join(map(edit, part.split('\n'))) for part in [head] + peers]


def conf(form):
    return ''.join(parts(form))


def wg_view(text):
    """config.c: [interface values, [peer values]]; each AllowedIPs line adds to the list (parse_allowedips)."""
    interface, peers, section = {}, [], None
    for raw in text.split('\n'):
        line = re.sub(r'[ \t\n\v\f\r]', '', raw.split('#', 1)[0])
        if not line:
            continue
        if line.encode().lower() in (b'[interface]', b'[peer]'):
            section = line.lower()
            if section == '[peer]':
                peers.append({})
            continue
        name, _, value = line.partition('=')
        name = name.encode().lower().decode()
        fields = interface if section == '[interface]' else peers[-1]
        if name == 'allowedips':
            fields.setdefault(name, []).extend(a for a in value.split(',') if a)
        else:
            fields[name] = value
    return [interface, peers]


def wg_quick_addresses(text):
    """wg-quick parse_options: `Address` of [Interface], `${value//,/ }` split by the shell."""
    addresses, interface = [], False
    for line in text.split('\n'):
        stripped = line.split('#', 1)[0]
        key = stripped.split('=', 1)[0].strip(' \t\n\v\f\r')
        value = stripped.split('=', 1)[-1].strip(' \t\n\v\f\r')
        if key.startswith('['):
            interface = key.lower() == '[interface]'
        if interface and key.lower() == 'address':
            addresses += [a for a in re.split(r'[ \t\n]', value.replace(',', ' ')) if a]
    return sorted('addr ' + a for a in addresses)


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


def python(block, **names):
    scope = {'re': re, 'os': os, 'hashlib': hashlib, **names}
    exec(block, scope)
    return scope


def restore_wg_key(form):
    """anysda-restore.sh wg_key(): fingerprint of the server key in a wg0.conf directory."""
    block = cut(source('infra/lib/anysda-restore.sh'), 'def digest(', '\n\n\ndef index_rows')
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / 'wg0.conf').write_text(conf(form))
        return python(block)['wg_key'](directory)


def restore_live_peers(form):
    """anysda-restore.sh: peer keys of the live /etc/wireguard/wg0.conf."""
    block = cut(source('infra/lib/anysda-restore.sh'), 'if live_peers is not None:\n',
                '\nif live_ike is not None:')
    return python(block, live_peers=conf(form))['live_peers']


def restore_drop_peers(form):
    """anysda-restore.sh: wg0.conf of the archive without the peer of a revoked device."""
    block = cut(source('infra/lib/anysda-restore.sh'), 'def rewrite(', '\n\nif (ike_ids')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'wg0.conf'
        path.write_text(conf(form))
        python(block, wg_keys={GONE}, wg_path=str(path))
        return path.read_text()


def backup_check(form):
    """anysda-backup.sh --check-bundle: complaints about WireGuard peers."""
    block = cut(source('infra/lib/anysda-backup.sh'), "wg = root / 'etc-wireguard/wg0.conf'", '\nswan = root')
    rows = [(1, True, (PEER, PSK_ONE, '10.66.66.2')),
            (2, None, (NEW, PSK_NEW, '10.66.66.3')),  # reissued: old key still on its address
            (3, True, (GONE, PSK_GONE, '10.66.66.4'))]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'etc-wireguard').mkdir()
        (root / 'etc-wireguard/wg0.conf').write_text(conf(form))
        scope = python(block, root=root, bad=[], retired_keys=set(), label=lambda d: f'устройство {d}',
                       device_columns={'wg_public_key', 'wg_preshared_key', 'wg_ip'},
                       devices=lambda *_fields: iter(rows))
    return scope['bad']


def load_verify():
    spec = importlib.util.spec_from_file_location('verify_traffic_more', ROOT / 'infra/lib/verify-traffic.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VT = load_verify()


def verify_conf_peers(form):
    """verify-traffic.py conf_peers(): peers as restore_wg_apply and wg.apply take them."""
    return [(p['key'], p['preshared_key'], p['allowed']) for p in VT.conf_peers(conf(form))]


def verify_syncconf_peers(form):
    """verify-traffic.py restore_wg_apply(): peers wg reads from `wg syncconf wg0 /dev/stdin`."""
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
    return wg_view(data)[1]


def svc_conf_iface(form):
    """anysda-svc.sh wg0_conf_iface: addresses wg0_apply compares with live wg0."""
    block = cut(source('infra/lib/anysda-svc.sh'), 'wg0_conf_iface() {', '\n}\n') + '\n}\n'
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'wg0.conf'
        path.write_text(conf(form))
        result = bash(block.replace('/etc/wireguard/wg0.conf', str(path)) + 'wg0_conf_iface\n', directory)
        return result.returncode, result.stdout.split('\n')[:-1]


def lines(text):
    return [line for line in text.split('\n') if line.strip(' \t\n\v\f\r')]


def wg_peers(form):
    return [(p['publickey'], p['presharedkey'], p['allowedips']) for p in wg_view(conf(form))[1]]


SERVER_HASH = hashlib.sha256(SERVER.encode()).hexdigest()
ADDRESSES = ['addr 10.66.66.1/24', 'addr fd66:66::1/64']
IN_PLACE = ('value-comment', 'blank-in-name', 'section-comment')


@as_root
class WgConfReadersMoreTests(unittest.TestCase):
    """1 restore in place, 2 AllowedIPs over two lines, 3 Address split like wg-quick, 4 the file as written."""

    def test_0_references_read_every_form_like_wg(self):
        view = wg_view(conf('canonical'))
        self.assertEqual(view[0]['privatekey'], SERVER)
        self.assertEqual(wg_peers('canonical')[2], (OLD, PSK_TWO, ['10.66.66.3/32', 'fd66:66::3/128']))
        for form in IN_PLACE + ('allowed-two-lines', 'address-space', 'address-two-lines'):
            with self.subTest(form):
                self.assertEqual(wg_view(conf(form))[1], view[1])
        for form in ('canonical', 'address-space', 'address-two-lines'):
            with self.subTest('wg-quick', form=form):
                self.assertEqual(wg_quick_addresses(conf(form)), ADDRESSES)

    def assert_dropped(self, form):
        """Only the revoked peer is gone; every other line stays as it was, blank lines aside."""
        text = restore_drop_peers(form)
        kept = ''.join(part for part in parts(form) if GONE not in part)
        self.assertEqual(lines(text), lines(kept))
        self.assertEqual([p['publickey'] for p in wg_view(text)[1]], [PEER, OLD])

    def test_1_restore_in_place_reads_like_wg(self):
        for form in IN_PLACE:
            with self.subTest('wg_key', form=form):
                self.assertEqual(restore_wg_key(form), SERVER_HASH)
            with self.subTest('live peers', form=form):
                self.assertEqual(restore_live_peers(form), {PEER, GONE, OLD})
            with self.subTest('drop revoked peer', form=form):
                self.assert_dropped(form)

    def test_2_two_allowedips_lines_keep_both_addresses(self):
        form = 'allowed-two-lines'
        with self.subTest('backup'):
            self.assertEqual(backup_check(form), [])
        with self.subTest('verify conf_peers'):
            self.assertEqual(verify_conf_peers(form), wg_peers(form))
        with self.subTest('verify syncconf body'):
            self.assertEqual(verify_syncconf_peers(form), wg_view(conf(form))[1])

    def test_3_svc_addresses_like_wg_quick(self):
        for form in ('address-space', 'canonical', 'address-two-lines'):
            with self.subTest(form):
                self.assertEqual(svc_conf_iface(form), (0, wg_quick_addresses(conf(form))))

    def test_4_file_as_written_reads_as_before(self):
        form = 'canonical'
        with self.subTest('restore wg_key'):
            self.assertEqual(restore_wg_key(form), SERVER_HASH)
        with self.subTest('restore live peers'):
            self.assertEqual(restore_live_peers(form), {PEER, GONE, OLD})
        with self.subTest('restore drop revoked peer'):
            self.assert_dropped(form)
        with self.subTest('backup'):
            self.assertEqual(backup_check(form), [])
        with self.subTest('verify conf_peers'):
            self.assertEqual(verify_conf_peers(form), wg_peers(form))
        with self.subTest('verify syncconf body'):
            self.assertEqual(verify_syncconf_peers(form), wg_view(conf(form))[1])
        with self.subTest('svc'):
            self.assertEqual(svc_conf_iface(form), (0, ADDRESSES))


if __name__ == '__main__':
    unittest.main()
