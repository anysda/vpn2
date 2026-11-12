"""Тонкие команды vpn2: что видит оператор на пустом, битом и повторном вызове."""
import json
import os

import pytest

from vpn2 import cli

UFW = r'''#!{python}
import shlex, sys
state = {state!r}
args = sys.argv[1:]
lines = [line for line in open(state).read().splitlines() if line]
open(state + '.calls', 'a').write(shlex.join(args) + '\n')
if args == ['show', 'added']:
    if 'BROKEN' in lines:
        sys.stderr.write('ERROR: problem running ufw\n')
        sys.exit(1)
    print("Added user rules (see 'ufw status' for running firewall):")
    print('\n'.join(lines))
elif args[:2] == ['--force', 'delete']:
    spec = args[2:]
    lines = [line for line in lines if shlex.split(line)[1:len(spec) + 1] != spec
             or len(shlex.split(line)) not in (len(spec) + 1, len(spec) + 3)]
    open(state, 'w').write('\n'.join(lines) + '\n')
else:
    lines.append('ufw ' + shlex.join(args))
    open(state, 'w').write('\n'.join(lines) + '\n')
'''


def test_dispatcher_refuses_an_unknown_command(capsys):
    assert cli.main(['no-such']) == 2
    assert capsys.readouterr().err.startswith('vpn2: неизвестная команда no-such\n')


def test_dispatcher_help_goes_to_stdout(capsys):
    assert cli.main(['--help']) == 0
    out, err = capsys.readouterr()
    assert out.startswith('Использование: vpn2 <команда> [аргументы]\n') and err == ''


def goog(tmp_path, data):
    path = tmp_path / 'goog.json'
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return str(path)


GOOG = {'prefixes': [{'ipv4Prefix': '192.0.2.0/25'}, {'ipv4Prefix': '192.0.2.128/25'},
                     {'ipv6Prefix': '2001:db8::/32'}]}


def test_nft_table_is_printed_with_collapsed_prefixes(tmp_path, capsys):
    path = goog(tmp_path, GOOG)
    assert cli.main(['nft', 'yt-quic', path]) == 0
    first = capsys.readouterr().out
    assert first.startswith('#!/usr/sbin/nft -f\n')
    assert 'elements = { 192.0.2.0/24 }' in first and 'elements = { 2001:db8::/32 }' in first
    assert cli.main(['nft', 'yt-quic', path]) == 0
    assert capsys.readouterr().out == first


def test_nft_without_file_uses_package_list(capsys):
    assert cli.main(['nft', 'yt-quic']) == 0
    assert capsys.readouterr().out.startswith('#!/usr/sbin/nft -f\n')


@pytest.mark.parametrize('argv', [[], ['other', 'x'], ['yt-quic', 'a', 'b']])
def test_nft_wrong_call_shows_usage(argv, capsys):
    assert cli.main(['nft', *argv]) == 2
    out, err = capsys.readouterr()
    assert out == '' and err == 'Использование: vpn2 nft yt-quic [goog.json]\n'


@pytest.mark.parametrize(('data', 'reason'), [
    ('', 'Expecting value'),
    ('{broken', 'Expecting property name'),
    ('[]', 'ожидается объект со списком prefixes'),
    ({'prefixes': []}, 'требуются непустые списки IPv4 и IPv6'),
    ({'prefixes': [{'ipv4Prefix': '192.0.2.0/24'}]}, 'требуются непустые списки IPv4 и IPv6'),
    ({'prefixes': [{'ipv4Prefix': '192.0.2.0/24', 'ipv6Prefix': '2001:db8::/32'}]}, 'ровно один префикс'),
    ({'prefixes': [{'ipv4Prefix': '192.0.2.1/24'}, {'ipv6Prefix': '2001:db8::/32'}]}, 'host bits'),
    ({'prefixes': [{'ipv4Prefix': '0.0.0.0/0'}, {'ipv6Prefix': '2001:db8::/32'}]}, 'маршрут по умолчанию'),
])
def test_nft_broken_list_prints_nothing_and_names_the_file(tmp_path, capsys, data, reason):
    path = goog(tmp_path, data)
    assert cli.main(['nft', 'yt-quic', path]) == 1
    out, err = capsys.readouterr()
    assert out == ''
    assert err.startswith(f'ERROR: {path}: ') and reason in err


def test_nft_missing_file_is_named(tmp_path, capsys):
    path = str(tmp_path / 'absent.json')
    assert cli.main(['nft', 'yt-quic', path]) == 1
    out, err = capsys.readouterr()
    assert out == '' and err.startswith('ERROR: ') and path in err


@pytest.mark.parametrize('argv', [[], ['no-such-step']])
def test_router_wrong_call_shows_every_step(argv, capsys):
    assert cli.main(['router', *argv]) == 2
    err = capsys.readouterr().err
    assert err == 'Использование: vpn2 router entry-addrs|apply-routes [аргументы]\n'


@pytest.mark.parametrize('stage', ['monitoring', 'telegram', 'panel'])
def test_containers_stage_is_skipped_outside_entry(stage, monkeypatch, capsys):
    monkeypatch.setenv('HOST_TAG', 'de')
    for _ in range(2):
        assert cli.main(['containers', stage]) == 0
        assert capsys.readouterr().out == f'[de] {stage}: только для entry - пропуск\nchanged=0\n'


@pytest.mark.parametrize(('stage', 'name', 'reason'), [
    ('monitoring', 'EXIT_TAGS', 'EXIT_TAGS пуст'),
    ('panel', 'ENTRY_HOST', 'ENTRY_HOST не задан'),
])
def test_containers_stage_names_the_missing_setting(stage, name, reason, monkeypatch, capsys):
    monkeypatch.setenv('HOST_TAG', 'ru')
    monkeypatch.delenv(name, raising=False)
    assert cli.main(['containers', stage]) == 1
    out, err = capsys.readouterr()
    assert err == f'vpn2 containers {stage}: {reason}\n' and 'changed=' not in out


def test_containers_unknown_stage_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(['containers', 'everything'])
    assert stop.value.code == 2
    assert 'monitoring' in capsys.readouterr().err


@pytest.mark.parametrize(('argv', 'env', 'missing'), [
    ([], {}, 'HOST_TAG, MGMT_IP'),
    (['--target', 'de'], {}, 'MGMT_IP'),
    (['--traffic'], {'HOST_TAG': 'ru'}, 'MGMT_IP'),
    ([], {'MGMT_IP': '10.0.0.1'}, 'HOST_TAG'),
])
def test_verify_without_node_settings_names_them(argv, env, missing, monkeypatch, capsys):
    for name in ('HOST_TAG', 'MGMT_IP'):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert cli.main(['verify', *argv]) == 1
    assert capsys.readouterr().err == f'verify: не задано {missing}\n'


def test_restore_help_prints_usage_and_touches_nothing(capsys):
    assert cli.main(['restore', '--help']) == 0
    out, err = capsys.readouterr()
    assert out.startswith('Использование: vpn2 restore [АРХИВ|latest]') and '--dry-run' in out and err == ''


def test_backup_without_settings_file_names_it(tmp_path, capsys):
    path = tmp_path / 'backup.env'
    for _ in range(2):
        assert cli.main(['backup', '--env-file', str(path)]) == 2
        assert capsys.readouterr().err == f'anysda-backup: {path} отсутствует или unreadable\n'
    assert not path.exists()


def test_backup_with_empty_settings_names_the_missing_backend(tmp_path, capsys):
    path = tmp_path / 'backup.env'
    path.write_text('')
    assert cli.main(['backup', '--env-file', str(path)]) != 0
    assert f'BACKUP_BACKEND не задан в {path}' in capsys.readouterr().err


def test_backup_unknown_flag_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(['backup', '--everything'])
    assert stop.value.code == 2
    assert capsys.readouterr().err == 'vpn2 backup: unrecognized arguments: --everything\n'


def test_backup_check_of_a_plain_directory_is_refused(tmp_path, capsys):
    assert cli.main(['backup', '--check-bundle', str(tmp_path)]) == 2
    assert 'не распакованный бандл (нет manifest.json)' in capsys.readouterr().err
    assert os.listdir(tmp_path) == []

