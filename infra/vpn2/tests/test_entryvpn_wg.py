"""Шаги роли wireguard: исходы сверены со стадией 28-wireguard.sh на тех же каталогах."""
import os
import shutil
import subprocess
import types

import pytest

from vpn2 import entryvpn, pki, wg

pytestmark = pytest.mark.skipif(not shutil.which('wg'), reason='нет wg')

KEY = 'YEocP0e2o1WT5GlvBvQzVF7EeR1z9aLk9SIGqBqLHFQ='
INITIAL = ('# Initial config — panel will overwrite via syncWireguardConfig().\n[Interface]\n'
           'Address = 10.66.66.1/24, fd66:66::1/64\nListenPort = 51820\nPrivateKey = {key}\n')


def tree(root):
    out = {}
    for path in sorted(root.rglob('*')):
        name = str(path.relative_to(root))
        if path.is_symlink():
            out[name] = '-> ' + os.readlink(path)
        elif path.is_file():
            out[name] = (path.read_text(), path.stat().st_mode & 0o777)
    return out


def identity(root):
    return entryvpn.wg_identity('51820', '10.66.66.1', root=root)


def shape(key, conf):
    public = wg.pubkey(key)
    return {'keys/server.priv': (key + '\n', 0o600), 'keys/server.pub': (public + '\n', 0o600),
            'server.priv': '-> keys/server.priv', 'server.pub': '-> keys/server.pub',
            'wg0.conf': conf, '.keys.lock': ('', 0o600)}


def test_clean_root_gets_new_key_and_initial_conf(tmp_path):
    assert identity(tmp_path) is True
    key = (tmp_path / 'keys/server.priv').read_text().strip()
    assert wg.valid_key(key)
    assert tree(tmp_path) == shape(key, (INITIAL.format(key=key), 0o600))
    assert identity(tmp_path) is False


def test_flat_key_migrates_unchanged(tmp_path):
    (tmp_path / 'server.priv').write_text(KEY + '\n')
    assert identity(tmp_path) is True
    assert tree(tmp_path) == shape(KEY, (INITIAL.format(key=KEY), 0o600))


@pytest.mark.parametrize('line', ['PrivateKey = {key}', 'privatekey={key} # старый', ' PRIVATEKEY\t=\t{key}'])
def test_key_is_taken_from_interface_of_existing_conf(tmp_path, line):
    conf = f'[Interface]\nAddress = 10.66.66.1/24\n{line.format(key=KEY)}\n[Peer]\nPublicKey = x\n'
    (tmp_path / 'wg0.conf').write_text(conf)
    (tmp_path / 'wg0.conf').chmod(0o640)
    assert identity(tmp_path) is True
    assert tree(tmp_path) == shape(KEY, (conf, 0o640))
    assert identity(tmp_path) is False


@pytest.mark.parametrize('conf', [
    '[Interface]\nAddress = 10.66.66.1/24\n',
    '[Interface]\nAddress = 10.66.66.1/24\n[Peer]\nPrivateKey = ' + KEY + '\n',
    '[Interface]\nPrivateKey = ' + KEY[:-2] + '\n',
])
def test_conf_without_usable_key_is_refused(tmp_path, conf):
    (tmp_path / 'wg0.conf').write_text(conf)
    (tmp_path / 'wg0.conf').chmod(0o644)
    with pytest.raises(pki.PkiIdentity, match='cannot replace an active WireGuard private key'):
        identity(tmp_path)
    assert tree(tmp_path) == {'wg0.conf': (conf, 0o644), '.keys.lock': ('', 0o600)}


def test_broken_key_with_public_half_is_refused(tmp_path):
    (tmp_path / 'keys').mkdir()
    (tmp_path / 'keys/server.priv').write_text('обрыв\n')
    (tmp_path / 'keys/server.pub').write_text('pub\n')
    for name in ('server.priv', 'server.pub'):
        os.symlink(f'keys/{name}', tmp_path / name)
    with pytest.raises(pki.PkiIdentity):
        identity(tmp_path)
    assert (tmp_path / 'keys/server.priv').read_text() == 'обрыв\n'
    assert not (tmp_path / 'wg0.conf').exists()


def test_empty_key_file_without_identity_is_regenerated(tmp_path):
    (tmp_path / 'keys').mkdir()
    (tmp_path / 'keys/server.priv').write_text('')
    assert identity(tmp_path) is True
    assert wg.valid_key((tmp_path / 'keys/server.priv').read_text().strip())


def runner(answers, calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs.get('input')))
        code, out = answers.get(tuple(argv[:3]), (0, ''))
        if kwargs.get('check') and code:
            raise subprocess.CalledProcessError(code, argv)
        return types.SimpleNamespace(returncode=code, stdout=out)
    return run


LIVE = ('5: wg0    inet 10.66.66.1/24 scope global wg0\\       valid_lft forever\n'
        '5: wg0    inet6 fd66:66::1/64 scope global \\       valid_lft forever\n')


@pytest.mark.parametrize('answers, address, restarted', [
    ({}, '10.66.66.1/24, fd66:66::1/64', False),
    ({}, 'fd66:66::1/64,10.66.66.1/24', False),
    ({}, '10.66.66.1/24', True),
    ({}, '10.66.60.1/24, fd66:66::1/64', True),
    ({('systemctl', 'is-active', '--quiet'): (3, '')}, '10.66.66.1/24, fd66:66::1/64', True),
    ({('ip', 'link', 'show'): (1, '')}, '10.66.66.1/24, fd66:66::1/64', True),
])
def test_apply_restarts_only_when_addresses_differ(tmp_path, answers, address, restarted):
    conf = tmp_path / 'wg0.conf'
    conf.write_text(f'[Interface]\nAddress = {address}\nPrivateKey = {KEY}\n')
    calls, said = [], []
    answers = {('ip', '-o', 'addr'): (0, LIVE), ('wg-quick', 'strip', 'wg0'): (0, b'[Interface]\n'), **answers}
    assert entryvpn.wg_apply(said.append, conf=conf, run=runner(answers, calls)) is restarted
    tail = calls[-1]
    if restarted:
        assert tail == (['systemctl', 'restart', 'wg-quick@wg0'], None)
        assert said == ['wg-quick@wg0 перезапущен']
    else:
        assert tail == (['wg', 'syncconf', 'wg0', '/dev/stdin'], b'[Interface]\n')
        assert said == ['адреса wg0 прежние — wg syncconf без рестарта']


def test_routing_ensure_restarts_unit_only_without_chain():
    calls, said = [], []
    args = ('anysda-wg-routing', 'wg0', 'ANYSDA_WG_TPROXY')
    assert entryvpn.routing_ensure(*args, said.append, run=runner({}, calls)) is False
    assert calls == [(['iptables', '-t', 'mangle', '-C', 'PREROUTING', '-i', 'wg0', '-j', 'ANYSDA_WG_TPROXY'], None)]
    calls.clear()
    lost = {('iptables', '-t', 'mangle'): (1, '')}
    assert entryvpn.routing_ensure(*args, said.append, run=runner(lost, calls)) is True
    assert calls[-1] == (['systemctl', 'restart', 'anysda-wg-routing'], None)
    assert said == ['anysda-wg-routing: цепочка ANYSDA_WG_TPROXY пропала, восстанавливаю']


def test_dns_rules_match_stage_order():
    assert entryvpn.dns_rules('10.99.0.1', ['10.66.66.0/24', '10.66.64.0/24']) == [
        'proto udp from 10.66.66.0/24 to 10.99.0.1 port 53', 'proto tcp from 10.66.66.0/24 to 10.99.0.1 port 53',
        'proto udp from 10.66.64.0/24 to 10.99.0.1 port 53', 'proto tcp from 10.66.64.0/24 to 10.99.0.1 port 53']
