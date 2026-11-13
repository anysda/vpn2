"""Шаги роли zapret: выбор шага, значения для скрипта проверки."""

import io

from envs import CONFIG, node_env
import pytest

from vpn2.zapret import stage


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
    monkeypatch.setattr(stage, 'source', lambda _environ, _cfg: True)
    monkeypatch.setattr(stage.os, 'environ', {})
    assert stage.main(['source']) == 0
    assert capsys.readouterr().out == 'changed=1\n'
    monkeypatch.setattr(stage.os, 'environ', {'YT_QUIC': 'maybe'})
    assert stage.main(['source']) == 1
    assert 'YT_QUIC' in capsys.readouterr().err


@pytest.mark.parametrize(('development', 'manifest'), [('true', []), ('false', ['--manifest', '/m.json'])])
def test_source_archive_is_pinned_with_or_without_manifest(monkeypatch, tmp_path, development, manifest):
    """В режиме разработки манифеста выпуска нет: архив берётся по закреплённому хешу."""
    calls = []
    monkeypatch.setattr(stage.release, 'main', lambda argv: calls.append(argv) or 1)
    environ = {'VPN2_DEVELOPMENT': development, 'VPN2_RELEASE_MANIFEST': '/m.json'}
    cfg = {'base': str(tmp_path), 'ref': stage.ZAPRET_REF, 'repo': stage.ZAPRET_REPO, 'bin': str(tmp_path / 'nfqws2')}
    with pytest.raises(stage.StageError, match='архив zapret2'):
        stage.source(environ, cfg)
    argv, = calls
    assert argv[:len(manifest) + 2] == [*manifest, 'artifact', 'zapret2-source']
    assert argv[argv.index('--sha256') + 1] == stage.ZAPRET_SHA256


SPLIT = '--payload=tls_client_hello --lua-desync=multisplit:pos=10,midsld:seqovl=1'


def window(**settings):
    """(стратегия, правила nft) при масштабе окна ядра 9 (VPN2-148)."""
    cfg = stage.settings({'YT_ZAPRET_WSCALE': '9', **settings})
    return cfg['strategy'], stage.window_rules(cfg, 'eth0')


def test_window_compat_default():
    strategy, rules = window()
    assert strategy == '--lua-desync=wssize:wsize=1:scale=1 ' + SPLIT
    assert 'tcp window > 255 counter tcp window set 65535' in rules and 'tcp window << 8' in rules
    assert all(line.startswith('        meta mark 256 oifname "eth0" tcp dport 443 ') for line in rules.splitlines())


@pytest.mark.parametrize('scale, strategy, rules', [
    ('native', '--lua-desync=wssize:wsize=1 ' + SPLIT, ''),
    ('9', '--lua-desync=wssize:wsize=1:scale=9 ' + SPLIT, ''),
    ('8', '--lua-desync=wssize:wsize=1:scale=8 ' + SPLIT, 'tcp window > 32767 counter tcp window set 65535'),
])
def test_window_scale(scale, strategy, rules):
    found = window(YT_ZAPRET_WINDOW_SCALE=scale)
    assert found[0] == strategy and rules in found[1] and (rules or found[1] == '')
    if scale == '8':
        assert 'tcp window << 1' in found[1]


def test_window_requested_scale_above_kernel_and_measured_kernel():
    assert window(YT_ZAPRET_WSCALE='6', YT_ZAPRET_STRATEGY='--lua-desync=wssize:wsize=1:scale=9 ' + SPLIT)[1] == ''
    cfg = stage.settings({})
    assert 'tcp window << 6' in stage.window_rules(cfg, 'eth0', measure=lambda: '7')
    # Замер не удался - берётся масштаб прода.
    assert 'tcp window << 8' in stage.window_rules(cfg, 'eth0', measure=lambda: '')


def test_window_custom_strategy_wins():
    custom = '--lua-desync=wssize:wsize=1:scale=1 ' + SPLIT
    strategy, rules = window(YT_ZAPRET_WINDOW_SCALE='native', YT_ZAPRET_STRATEGY=custom)
    assert strategy == custom and 'tcp window << 8' in rules
    assert window(YT_ZAPRET_STRATEGY=SPLIT) == (SPLIT, '')


@pytest.mark.parametrize('scale', ['15', '-1', '09', 'auto', '1 --new'])
def test_window_invalid_scale_rejected(scale):
    with pytest.raises(stage.StageError, match='YT_ZAPRET_WINDOW_SCALE'):
        window(YT_ZAPRET_WINDOW_SCALE=scale)


def test_empty_values_fall_back_to_defaults():
    cfg = stage.settings({'YT_ROUTE': '', 'YT_QUIC': '', 'YT_MARK': '', 'YT_ZAPRET_BASE': ''})
    assert (cfg['route'], cfg['quic'], cfg['mark'], cfg['bin']) == ('off', 'block', '256', '/opt/zapret2/binaries/my/nfqws2')
    with pytest.raises(stage.StageError, match='YT_QUIC'):
        stage.settings({'YT_QUIC': 'maybe'})


@pytest.mark.parametrize('scale', ['native', '0', '1', '8', '9', '14', '15', '-1', '09', 'auto'])
def test_window_scale_from_config(tmp_path, scale):
    prepared, env = node_env(tmp_path, CONFIG + f'\nyoutube:\n  route: zapret\n  window_scale: {scale}\n')
    if scale in ('15', '-1', '09', 'auto'):
        assert env is None and 'youtube.window_scale' in prepared.stderr
    else:
        assert env['YT_ZAPRET_WINDOW_SCALE'] == scale, prepared.stderr


@pytest.mark.parametrize('argv, stdin, code, out', [
    (['queued', '200'], '{"nftables": [{"rule": {"expr": [{"queue": {"num": 200}}, {"counter": {"packets": 7}}]}}]}', 0, '7\n'),
    (['queued', '200'], '{"nftables": [{"rule": {"expr": [{"queue": {"num": 201}}, {"counter": {"packets": 7}}]}}]}', 1, ''),
    (['queued', '200'], 'not json', 1, ''),
    (['queued', 'x'], '{"nftables": []}', 1, '')])
def test_queued_counts_packets_of_the_queue_only(monkeypatch, capsys, argv, stdin, code, out):
    monkeypatch.setattr('sys.stdin', io.StringIO(stdin))
    assert stage.main(argv) == code
    assert capsys.readouterr().out == out
