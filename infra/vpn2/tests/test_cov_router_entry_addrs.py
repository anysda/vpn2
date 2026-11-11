"""Набор entry-addrs перед стартом sing-box: адреса берутся у `ip`, файл меняется только по делу."""
import json
import os

import pytest

from vpn2.router import entry_addrs

LINKS = [
    {'ifname': 'lo', 'addr_info': [{'local': '127.0.0.1', 'scope': 'host'}]},
    {'ifname': 'eth0', 'addr_info': [
        {'local': '2001:db8::5', 'scope': 'global'},
        {'local': '203.0.113.7', 'scope': 'global'},
        {'local': 'fe80::1', 'scope': 'link'},
        {'scope': 'global'},
    ]},
    {'ifname': 'wg0', 'addr_info': [{'local': '10.66.66.1', 'scope': 'global'},
                                    {'local': '203.0.113.7', 'scope': 'global'}]},
    {'ifname': 'dummy0'},
]
NETS = ['10.66.66.1/32', '203.0.113.7/32', '2001:db8::5/128']


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Каталог набора и подменная команда ip: печатает файл answer или падает без него."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    answer = tmp_path / 'answer'
    ip = bin_dir / 'ip'
    ip.write_text(f'#!/bin/sh\necho "$*" > {tmp_path}/args\nexec cat {answer}\n')
    ip.chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
    box = tmp_path / 'sing-box'
    box.mkdir()
    monkeypatch.setattr(entry_addrs, 'SET', str(box / 'entry-addrs.json'))
    answer.write_text(json.dumps(LINKS))
    return tmp_path


def rules(host):
    return json.loads((host / 'sing-box/entry-addrs.json').read_text())


def failed(capsys):
    with pytest.raises(SystemExit) as stop:
        entry_addrs.main()
    assert stop.value.code == 1
    return capsys.readouterr().err


def test_global_addresses_only_sorted_and_once(host):
    assert entry_addrs.addresses() == NETS
    assert (host / 'args').read_text().split() == ['-j', 'addr', 'show', 'scope', 'global']


def test_set_is_written_private_and_named_in_the_log(host, capsys):
    assert entry_addrs.main() == 0
    assert rules(host) == {'version': 2, 'rules': [{'ip_cidr': NETS}]}
    path = host / 'sing-box/entry-addrs.json'
    assert path.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out.strip() == 'vpn2-entry-addrs: клиентам закрыты службы на ' + ' '.join(NETS)


def test_repeat_leaves_the_file_alone(host):
    entry_addrs.main()
    path = host / 'sing-box/entry-addrs.json'
    before = path.stat()
    entry_addrs.main()
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_new_address_replaces_the_set_without_leftovers(host):
    entry_addrs.main()
    (host / 'answer').write_text(json.dumps([{'addr_info': [{'local': '198.51.100.9', 'scope': 'global'}]}]))
    entry_addrs.main()
    assert rules(host)['rules'] == [{'ip_cidr': ['198.51.100.9/32']}]
    assert os.listdir(host / 'sing-box') == ['entry-addrs.json']


def test_host_without_global_addresses_gets_an_empty_set(host, capsys):
    (host / 'answer').write_text('[]')
    assert entry_addrs.main() == 0
    assert rules(host) == {'version': 2, 'rules': []}
    assert capsys.readouterr().out.strip().endswith('службы на -')


@pytest.mark.parametrize('answer', [None, '', '{broken', '[{"addr_info": [{"local": "not-an-ip", "scope": "global"}]}]'])
def test_unreadable_addresses_on_first_start_give_an_empty_set(host, capsys, answer):
    if answer is None:
        (host / 'answer').unlink()
    else:
        (host / 'answer').write_text(answer)
    err = failed(capsys)
    assert 'адреса entry не прочитаны' in err and err.strip().endswith('набор пуст')
    assert rules(host) == {'version': 2, 'rules': []}


def test_unreadable_addresses_keep_the_set_of_the_last_start(host, capsys):
    entry_addrs.main()
    kept = (host / 'sing-box/entry-addrs.json').read_text()
    (host / 'answer').write_text('{broken')
    err = failed(capsys)
    assert err.strip().endswith('набор прошлого старта')
    assert (host / 'sing-box/entry-addrs.json').read_text() == kept


def test_missing_ip_command_is_the_same_refusal(host, capsys, monkeypatch):
    monkeypatch.setenv('PATH', str(host / 'nowhere'))
    assert 'адреса entry не прочитаны' in failed(capsys)
    assert rules(host)['rules'] == []
