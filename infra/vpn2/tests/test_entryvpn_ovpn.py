"""Шаги роли openvpn: PKI сервера и рестарт по входам."""
from pathlib import Path
import types

import pytest

from vpn2 import entryvpn, pki


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
