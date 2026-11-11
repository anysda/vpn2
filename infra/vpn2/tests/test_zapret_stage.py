"""Шаги роли zapret: готовность очереди, выбор шага, значения для скрипта проверки."""

import pytest

from vpn2.zapret import stage


def test_queue_is_bound_only_by_own_daemon(tmp_path):
    table = tmp_path / 'queue'
    table.write_text('200 123 0 2 65531 0 0 1 1\n')
    assert stage.queue_bound('200', '123', table)
    assert not stage.queue_bound('200', '456', table)
    assert not stage.queue_bound('201', '123', table)
    assert not stage.queue_bound('200', '', table)
    assert not stage.queue_bound('200', '123', tmp_path / 'absent')


def test_shell_values_stay_data():
    values = stage.shell_values({'mark': '256', 'queue': '200', 'base': '/opt/x $(touch INJECTED)'})
    assert values == {'mark_sh': '256', 'queue_sh': '200', 'base_sh': "'/opt/x $(touch INJECTED)'"}


def test_lua_modules_are_required(tmp_path):
    (tmp_path / 'lua').mkdir()
    for module in stage.LUA_MODULES[:-1]:
        (tmp_path / 'lua' / f'{module}.lua').touch()
    with pytest.raises(stage.StageError, match='zapret-auto'):
        stage.lua_init({'base': str(tmp_path)})
    (tmp_path / 'lua/zapret-auto.lua').touch()
    assert stage.lua_init({'base': str(tmp_path)}).split() == [
        f'--lua-init=@{tmp_path}/lua/{module}.lua' for module in stage.LUA_MODULES]


def test_step_prints_change_and_reports_bad_settings(monkeypatch, capsys):
    monkeypatch.setattr(stage, 'quic_off', lambda: True)
    monkeypatch.setattr(stage.os, 'environ', {})
    assert stage.main(['quic-off']) == 0
    assert capsys.readouterr().out == 'changed=1\n'
    monkeypatch.setattr(stage.os, 'environ', {'YT_QUIC': 'maybe'})
    assert stage.main(['quic-off']) == 1
    assert 'YT_QUIC' in capsys.readouterr().err
