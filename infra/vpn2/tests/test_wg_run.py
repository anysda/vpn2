"""vpn2.wg: внешний wg (через подмену run), рендер и разбор вывода `wg show`. Ожидания сняты со старого кода."""
import base64
import shutil
import subprocess

import pytest

from vpn2 import wg
from vpn2.wg import WgError, WgUnchecked


def make_key(seed):
    return base64.b64encode(bytes((seed * 7 + i) % 256 for i in range(32))).decode()


A, B, C, S = (make_key(n) for n in (1, 2, 3, 4))


def clamp(key):
    raw = bytearray(base64.b64decode(key))
    raw[0] &= 248
    raw[31] = (raw[31] & 127) | 64
    return base64.b64encode(raw).decode()


def done(code=0, out='', err=''):
    return subprocess.CompletedProcess([], code, out.encode(), err.encode())


class Fake:
    """Подмена run: отвечает по очереди, помнит argv, stdin и таймаут."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, argv, *, data=None, timeout=30):
        self.calls.append((list(argv), data, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


CONF = f'[Interface]\nListenPort = 51820\nPrivateKey = {S}\n\n[Peer]\nPublicKey = {A}\nAllowedIPs = 192.0.2.2/32\n'
HEAD = f'{clamp(S)}\t{B}\t51820\toff\n'
DUMP = HEAD + f'{A}\t(none)\t(none)\t192.0.2.2/32\t0\t0\t0\toff\n'
REFUSE_FOO = "Line unrecognized: `Foo=1'\nConfiguration parsing error"


def test_run_defaults_and_str_input():
    result = wg.run(['cat'], data='привет', timeout=5)
    assert (result.returncode, result.stdout) == (0, 'привет'.encode())


@pytest.mark.parametrize(('func', 'name'), [(wg.genkey, 'genkey'), (wg.genpsk, 'genpsk')])
def test_genkey_genpsk(func, name):
    fake = Fake(done(out=A + '\n'), done(out=A + '\n'))
    assert func(run=fake) == func(run=fake) == A
    assert fake.calls[0][0] == ['wg', name]


def test_genkey_refusals():
    with pytest.raises(WgError, match='wg genkey: код 1'):
        wg.genkey(run=Fake(done(1)))
    with pytest.raises(WgError, match='не ключ'):
        wg.genkey(run=Fake(done(out='мусор')))
    with pytest.raises(WgUnchecked, match='нет wg'):
        wg.genkey(run=Fake(FileNotFoundError()))
    with pytest.raises(WgUnchecked, match='не ответил за 30 с'):
        wg.genpsk(run=Fake(subprocess.TimeoutExpired('wg', 30)))


def test_pubkey_private_only_via_stdin():
    fake = Fake(done(out=B + '\n'))
    assert wg.pubkey(S, run=fake) == B
    argv, data, _ = fake.calls[0]
    assert argv == ['wg', 'pubkey'] and data == S + '\n' and S not in ' '.join(argv)
    with pytest.raises(WgError) as raised:
        wg.pubkey(S, run=Fake(done(1, err=f'wg: bad {S}')))
    assert S not in str(raised.value) and 'код 1' in str(raised.value)


def test_render_interface_bytes():
    assert wg.render_interface(['192.0.2.1/24', 'fd66:66::1/64'], 51820, S, note='Initial config') == (
        f'# Initial config\n[Interface]\nAddress = 192.0.2.1/24, fd66:66::1/64\nListenPort = 51820\n'
        f'PrivateKey = {S}\n')
    assert wg.render_interface(['192.0.2.1/24'], 1, S) == (
        f'[Interface]\nAddress = 192.0.2.1/24\nListenPort = 1\nPrivateKey = {S}\n')


def test_render_peers_panel_and_check_forms():
    peers = [{'key': A, 'preshared_key': C, 'allowed': ['192.0.2.2/32', 'fd66:66::2/128']},
             {'key': B, 'preshared_key': '', 'allowed': []}]
    full = f'\n[Peer]\nPublicKey = {A}\nPresharedKey = {C}\nAllowedIPs = 192.0.2.2/32, fd66:66::2/128\n'
    assert wg.render_peers(peers) == full + f'\n[Peer]\nPublicKey = {B}\nPresharedKey = \nAllowedIPs = \n'
    assert wg.render_peers(peers, omit_empty=True) == full + f'\n[Peer]\nPublicKey = {B}\n'
    assert wg.render_peers([]) == '' == wg.render_peers([], omit_empty=True)


def test_rendered_file_passes_check_and_roundtrips():
    text = wg.render_interface(['192.0.2.1/24'], 51820, S) + wg.render_peers(
        [{'key': A, 'preshared_key': C, 'allowed': ['192.0.2.2/32']}])
    wg.check(text)
    assert wg.private_key(text) == S and wg.public_keys(text) == {A}
    assert wg.addresses(text) == ['192.0.2.1/24']


@pytest.mark.parametrize(('ip', 'expected'), [
    ('10.66.66.2', ['10.66.66.2/32', 'fd66:66::2/128']),
    ('10.66.5.9', ['10.66.5.9/32', 'fd66:66::5:9/128']),
    ('192.0.2.77', ['192.0.2.77/32', 'fd66:66::2:77/128']),
])
def test_peer_addresses(ip, expected):
    assert wg.peer_addresses(ip) == expected


def test_dump_success_keeps_file_private_key_and_stdin_only():
    fake = Fake(done(out=DUMP))
    rows = wg.dump(CONF, run=fake)
    # wg печатает прижатый ключ, в ответ идёт ключ файла.
    assert rows[0] == f'{S}\t{B}\t51820\toff'
    assert rows[1].startswith(f'{A}')
    argv, data, timeout = fake.calls[0]
    assert argv == list(wg.ASK) and timeout == 30 and S not in ' '.join(argv)
    assert data == CONF.encode()


def test_dump_takes_bytes_and_strips_wg_quick_names():
    fake = Fake(done(out=HEAD))
    wg.dump(CONF.replace('ListenPort', 'Address = 192.0.2.1/24\nListenPort').encode(), run=fake)
    assert b'Address' not in fake.calls[0][1]


def test_dump_refusal_retries_with_line_blanked():
    bad = CONF.replace('ListenPort = 51820\n', 'ListenPort = 51820\nFoo = 1\n')
    fake = Fake(done(121, err=REFUSE_FOO), done(out=HEAD))
    # Отказ, названный раньше, остаётся отказом, даже если со второго раза wg взял файл.
    with pytest.raises(WgError, match='строка 3 `Foo`'):
        wg.dump(bad, run=fake)
    assert b'Foo' in fake.calls[0][1] and b'Foo' not in fake.calls[1][1]
    fake = Fake(done(121, err=REFUSE_FOO), done(121, err='Invalid configuration\nConfiguration parsing error'))
    with pytest.raises(WgError, match=r'wg0\.conf: wg отвергает файл: строка 3 `Foo`: Line unrecognized; '):
        wg.dump(bad, run=fake)


def test_dump_refusal_never_names_a_key():
    secret = CONF.replace(S, S[:-2] + 'x=')
    fake = Fake(done(121, err=f"Key is not the correct length or format: `{S[:-2]}x='\n"
                              'Configuration parsing error'),
                done(121, err='Invalid configuration\nConfiguration parsing error'))
    with pytest.raises(WgError) as raised:
        wg.dump(secret, run=fake)
    assert S[:-2] not in str(raised.value) and 'wg0.conf' in str(raised.value)


@pytest.mark.parametrize(('answer', 'text'), [
    (done(120, err='sh: ip: not found'), 'wg0 в netns: sh: ip: not found'),
    (done(122, err='boom'), 'wg show: boom'),
    (done(1, err='unshare: failed\nx'), 'unshare --net: unshare: failed; x'),
    (done(121, err='something else'), 'wg setconf: something else'),
    (subprocess.TimeoutExpired('x', 1), 'setpriv не ответил за 30 с'),
    (FileNotFoundError(), 'нет setpriv'),
])
def test_dump_unchecked_cases(answer, text):
    with pytest.raises(WgUnchecked, match='wg0.conf: проверить нечем - ') as raised:
        wg.dump(CONF, run=Fake(answer))
    assert text in str(raised.value)


def test_dump_missing_programs_checked_for_real_run_only():
    with pytest.raises(WgUnchecked, match='нет wg, ip'):
        wg.dump(CONF, which=lambda name: None if name in ('wg', 'ip') else '/bin/' + name)
    # С подменой run проверка which не нужна: тест идёт без wg.
    assert wg.dump(CONF, run=Fake(done(out=HEAD)), which=lambda _name: None)


def test_dump_bad_saveconfig_is_wgerror_before_run():
    fake = Fake()
    with pytest.raises(WgError, match='SaveConfig'):
        wg.dump('[Interface]\nSaveConfig = maybe\n', run=fake)
    assert fake.calls == []


def test_read_and_conf_peers():
    out = HEAD + f'{A}\t{C}\t(none)\t192.0.2.2/32,fd66:66::2/128\t0\t0\t0\toff\n' \
                 f'{B}\t(none)\t(none)\t(none)\t0\t0\t0\toff\n'
    interface, peers = wg.read(CONF, run=Fake(done(out=out)))
    assert interface == {'PrivateKey': S, 'ListenPort': '51820'}
    assert peers == [{'key': A, 'preshared_key': C, 'allowed': ['192.0.2.2/32', 'fd66:66::2/128']},
                     {'key': B, 'preshared_key': '', 'allowed': []}]
    assert wg.conf_peers(CONF, run=Fake(done(out=out))) == peers
    # Без порта и fwmark: только заданное; повтор тот же.
    unset = f'{clamp(S)}\t{B}\t0\toff\n'
    for _ in range(2):
        assert wg.read(CONF, run=Fake(done(out=unset)))[0] == {'PrivateKey': S}


def test_read_fwmark_and_hidden_key():
    out = f'(none)\t{B}\t0\t0x1f\n'
    assert wg.read('x', run=Fake(done(out=out)))[0] == {'FwMark': '0x1f'}


def test_parse_dump_snapshot_forms():
    text = (f'wg0\t{S}\t{B}\t51820\toff\n'
            f'wg0\t{A}\t{C}\t203.0.113.5:4000\t10.8.0.3/32,10.8.0.9/32\t1700000000\t10\t20\toff\n'
            f'wg0\t{B}\t(none)\t(none)\t10.8.0.2/32\t0\t0\t0\toff\n'
            f'wg1\t{S}\t{B}\t51821\toff\n'
            f'wg1\t{C}\t(none)\t(none)\t(none)\t0\t0\t0\toff\n')
    assert wg.parse_dump(text) == {
        'wg0': {'listen_port': '51820', 'peers': ['10.8.0.2/32', '10.8.0.3/32,10.8.0.9/32']},
        'wg1': {'listen_port': '51821', 'peers': ['(none)']}}
    assert wg.parse_dump('') == {}


def test_allowed_preshared_transfer():
    table = f'{A}\t192.0.2.3/32 fd66:66::3/128\n{B}\t192.0.2.2/32,fd66:66::2/128\n'
    assert wg.allowed_ips(table) == {A: ['192.0.2.3/32', 'fd66:66::3/128'],
                                     B: ['192.0.2.2/32', 'fd66:66::2/128']}
    assert wg.allowed_ips('') == {}
    assert wg.preshared_keys(f'{A}\t{C}\n{B}\t(none)\n\n') == {A: C, B: '(none)'}
    # rx и tx переставлены: вид клиента.
    assert wg.transfer(f'{A}\t10\t20\n{B}\t0\t7\n') == [{'key': A, 'rx': 20, 'tx': 10},
                                                       {'key': B, 'rx': 7, 'tx': 0}]
    assert wg.transfer('') == []


@pytest.mark.parametrize('func', [wg.allowed_ips, wg.preshared_keys, wg.transfer])
def test_table_garbage_is_wgerror(func):
    with pytest.raises(WgError):
        func('одна строка без табуляции')
    with pytest.raises(WgError):
        func(f'{A}\t1\t2\t3\n')


def test_transfer_not_a_number():
    with pytest.raises(WgError, match='не число'):
        wg.transfer(f'{A}\tx\t1\n')


@pytest.mark.skipif(shutil.which('wg') is None, reason='нет wg')
def test_real_wg_keys():
    private, psk = wg.genkey(), wg.genpsk()
    assert wg.valid_key(private, nonzero=True) and wg.valid_key(psk, nonzero=True)
    assert wg.pubkey(private) == wg.pubkey(private) != private
    with pytest.raises(WgError):
        wg.pubkey('garbage')
