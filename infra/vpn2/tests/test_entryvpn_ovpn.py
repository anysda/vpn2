"""Шаги роли openvpn: server.conf сверен со стадией 29-openvpn.sh байт в байт."""
import hashlib
from pathlib import Path
import types

import pytest

from vpn2 import entryvpn, pki

POOL = ['10.67.67.0/24', '10.67.68.0/24']
# sha256 текста, который стадия писала heredoc-ом при тех же порте, сети и пуле.
STAGE_CONF = {
    'udp': '1bd687a3628ec3ce3779a9ba740c501b34344828b9b7214916804739f603d9e7',
    'tcp': '75dfd10d79690ff492e1c4b7222980b565a3357f533efcb40fd74532d39fef1c',
    'udp6': '7760eacd5c5719eb8b98ba1636b5062d5d5e47d28f46894725f81f1b4d261e85',
    'tcp4-server': '98f0026965801a1e13ccc6a2a7204b0fa979e67363c3042e33b5ee9511beacce',
}


@pytest.mark.parametrize('proto', sorted(STAGE_CONF))
def test_server_conf_is_the_text_of_the_stage(proto):
    text = entryvpn.ovpn_server_conf('1194', proto, '10.67.67.0', POOL, 'root')
    assert hashlib.sha256(text.encode()).hexdigest() == STAGE_CONF[proto]


def test_server_conf_follows_protocol_pool_and_user():
    text = entryvpn.ovpn_server_conf('1195', 'tcp', '10.67.67.0', POOL, 'vpn2-control')
    assert 'port 1195\nproto tcp-server\n' in text
    assert '\nfast-io\n' not in text
    assert 'management-client-user vpn2-control\n' in text
    assert text.endswith('verb 3\nroute 10.67.68.0 255.255.255.0\n')
    assert '\nfast-io\n' in entryvpn.ovpn_server_conf('1194', 'udp4', '10.67.67.0', POOL[:1], 'root')
    with pytest.raises(ValueError):
        entryvpn.ovpn_server_conf('1194', 'sctp', '10.67.67.0', POOL, 'root')


def test_write_conf_changes_only_with_inputs(tmp_path):
    marker = tmp_path / 'api-managed'

    def write(port='1194'):
        return entryvpn.ovpn_write_conf(port, 'udp', '10.67.67.0', POOL, directory=tmp_path, marker=marker)
    assert write() is True
    assert 'management-client-user root\n' in (tmp_path / 'server.conf').read_text()
    assert write() is False
    assert write('1195') is True
    # После миграции панели сокет остаётся за службой: повтор роли не возвращает root.
    marker.touch()
    assert write('1195') is True
    assert 'management-client-user vpn2-control\n' in (tmp_path / 'server.conf').read_text()
    assert write('1195') is False


def test_pki_makes_directories_and_reports_change(tmp_path, monkeypatch):
    built = []

    def build(work, *, run):
        built.append(run)
        (Path(work) / 'ca.crt').write_text('ca\n')
    monkeypatch.setattr(pki, 'build_openvpn', build)
    target, ccd = tmp_path / 'server/pki', tmp_path / 'server/ccd'
    assert entryvpn.ovpn_pki(target=target, dirs=(ccd,), run='runner') is True
    assert (target / 'newcerts').is_dir() and ccd.is_dir()
    assert (target / 'ca.crt').read_text() == 'ca\n'
    assert entryvpn.ovpn_pki(target=target, dirs=(ccd,), run='runner') is False
    assert built == ['runner', 'runner']


def no_sleep(seconds):
    del seconds


def restart(tmp_path, *, changed, active=0, mark=0, apply=True):
    """ovpn_restart с подставными systemctl и vpn2-apply: (сказанное, вызовы)."""
    tool = tmp_path / 'vpn2-apply'
    if apply:
        tool.write_text('')
        tool.chmod(0o755)
    calls, said = [], []

    def run(argv, **kwargs):
        del kwargs
        calls.append(argv)
        return types.SimpleNamespace(returncode=mark if argv[0] == str(tool) else active)
    entryvpn.ovpn_restart(lambda: changed, said.append, apply=str(tool), run=run, sleep=no_sleep)
    return said, calls, str(tool)


ACTIVE = ['systemctl', 'is-active', '--quiet', 'openvpn-server@server']


def test_restart_marks_running_conf_as_last_good(tmp_path):
    said, calls, tool = restart(tmp_path, changed=True)
    assert said == ['openvpn-server@server перезапущен']
    assert calls == [ACTIVE, [tool, 'mark', 'openvpn']]


def test_unchanged_inputs_are_probed_and_marked_too(tmp_path):
    said, calls, tool = restart(tmp_path, changed=False)
    assert said == ['openvpn-server@server: конфиг, сертификаты и бинарник прежние — не перезапускаю']
    assert calls == [ACTIVE, [tool, 'mark', 'openvpn']]


def test_first_install_without_vpn2_apply_marks_nothing(tmp_path):
    _, calls, _ = restart(tmp_path, changed=True, apply=False)
    assert calls == [ACTIVE]


@pytest.mark.parametrize('changed, probes', [(True, 10), (False, 1)])
def test_server_that_did_not_come_up_fails_without_mark(tmp_path, changed, probes):
    calls, said = [], []

    def run(argv, **kwargs):
        del kwargs
        calls.append(argv)
        return types.SimpleNamespace(returncode=3)
    with pytest.raises(RuntimeError, match='не поднялся'):
        entryvpn.ovpn_restart(lambda: changed, said.append, apply=str(tmp_path / 'vpn2-apply'), run=run, sleep=no_sleep)
    assert calls == [ACTIVE] * probes


def test_failed_mark_is_said_and_does_not_fail_the_step(tmp_path):
    said, _, _ = restart(tmp_path, changed=False, mark=1)
    assert said[-1] == 'vpn2-apply mark openvpn не прошёл: last good не совпадает с server.conf'
