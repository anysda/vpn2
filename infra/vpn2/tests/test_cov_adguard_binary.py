"""Шаги binary и legacy роли adguard: бинарник ставится только сверенным, старые правила ufw уходят один раз."""
import os
import sys

import pytest

from vpn2.adguard import stage

DEV = {'VPN2_DEVELOPMENT': 'true'}


def binary(version):
    return f'#!/bin/sh\necho "AdGuard Home, version {version}"\n'.encode()


@pytest.fixture
def target(tmp_path):
    directory = tmp_path / 'opt/AdGuardHome'
    directory.mkdir(parents=True)
    return directory


def put(target, body):
    (target / 'AdGuardHome').write_bytes(body)
    (target / 'AdGuardHome').chmod(0o755)
    return target / 'AdGuardHome'


def test_installed_binary_of_the_wanted_version_is_left_alone(target, capsys):
    installed = put(target, binary(stage.AGH_VER))
    before = installed.stat().st_mtime_ns
    for _ in range(2):
        assert stage.install_binary(DEV, target) is False
        assert capsys.readouterr().out == f'AdGuard Home {stage.AGH_VER} уже установлен\n'
    assert installed.stat().st_mtime_ns == before
    assert os.listdir(target) == ['AdGuardHome']


@pytest.mark.parametrize('old', [None, binary('v0.107.1'), b'#!/bin/sh\nexit 3\n', b'not a program'])
def test_archive_that_cannot_be_fetched_changes_nothing(target, monkeypatch, old, capfd):
    monkeypatch.setattr(stage, 'AGH_URL', (target / 'absent.tar.gz').as_uri())
    if old is not None:
        put(target, old)
    with pytest.raises(stage.StageError, match='архив AdGuard Home не получен'):
        stage.install_binary(DEV, target)
    assert 'no unverified artifact accepted' in capfd.readouterr().err
    assert os.listdir(target) == ([] if old is None else ['AdGuardHome'])
    if old is not None:
        assert (target / 'AdGuardHome').read_bytes() == old


def test_release_install_without_a_manifest_is_refused(target, tmp_path):
    environ = {'VPN2_RELEASE_MANIFEST': str(tmp_path / 'absent.json')}
    with pytest.raises((stage.StageError, OSError, SystemExit)):
        stage.install_binary(environ, target)
    assert os.listdir(target) == []


def test_version_of_nothing_is_empty(tmp_path):
    assert stage.installed_version(tmp_path / 'absent') == ''
    silent = tmp_path / 'silent'
    silent.write_text('#!/bin/sh\necho no version here\n')
    silent.chmod(0o755)
    assert stage.installed_version(silent) == ''


UFW = r'''#!{python}
import sys
state = {state!r}
rules = open(state).read().split('\n')
rule = ' '.join(sys.argv[2:])
if rule in rules:
    rules.remove(rule)
    open(state, 'w').write('\n'.join(rules))
    print('Rule deleted')
else:
    print('Could not delete non-existent rule')
'''


@pytest.fixture
def ufw(tmp_path, monkeypatch):
    state = tmp_path / 'ufw-rules'
    state.write_text('')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'ufw').write_text(UFW.format(python=sys.executable, state=str(state)))
    (bin_dir / 'ufw').chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir))
    return state


def rule(proto, bind='10.99.0.1'):
    return f'allow proto {proto} from 10.99.0.0/24 to {bind} port 53'


def test_legacy_rules_are_removed_once(ufw):
    ufw.write_text('\n'.join([rule('udp'), 'allow 22/tcp', rule('tcp')]))
    assert stage.legacy({}) is True
    assert ufw.read_text() == 'allow 22/tcp'
    assert stage.legacy({}) is False


def test_legacy_follows_the_management_address(ufw):
    ufw.write_text(rule('udp', '10.99.0.7'))
    assert stage.legacy({'MGMT_IP': '10.99.0.1'}) is False
    assert stage.legacy({'MGMT_IP': '10.99.0.7'}) is True
    assert ufw.read_text() == ''


def test_legacy_step_reports_changed_last(ufw, capsys):
    ufw.write_text(rule('tcp'))
    assert stage.main(['legacy']) == 0
    assert capsys.readouterr().out.splitlines()[-1] == 'changed=1'
    assert stage.main(['legacy']) == 0
    assert capsys.readouterr().out.splitlines()[-1] == 'changed=0'


def test_step_without_its_program_is_an_error_for_the_operator(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('PATH', str(tmp_path))
    assert stage.main(['legacy']) == 1
    out, err = capsys.readouterr()
    assert err.startswith('vpn2 adguard legacy: FileNotFoundError') and 'changed=' not in out


def test_unknown_step_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as stop:
        stage.main(['everything'])
    assert stop.value.code == 2
    assert 'binary' in capsys.readouterr().err
