"""Закрытые конфиги наблюдателей аварийного канала: один на узел, общая личность не меняется при повторе."""
import json
import os

import pytest

pytest.importorskip('yaml')

from vpn2.emergency import provision  # noqa: E402


def root_with(tmp_path, text):
    (tmp_path / 'config.yaml').write_text(text)
    return tmp_path


def config(tags):
    exits = ''.join(f'  - {{tag: {tag}, host: 203.0.113.{n}}}\n' for n, tag in enumerate(tags, 1))
    return 'entry: {host: 198.51.100.1}\nexits:' + (' []\n' if not tags else '\n' + exits)


def files(root):
    return sorted(os.listdir(root / 'infra/secrets/emergency'))


def load(root, name):
    return json.loads((root / 'infra/secrets/emergency' / name).read_text())


def test_every_node_gets_its_private_config(tmp_path):
    root = root_with(tmp_path, config(['se', 'entry']))
    provision.main([str(root)])
    assert files(root) == ['.lock', 'entry.json', 'identity.json', 'ru.json', 'se.json']
    directory = root / 'infra/secrets/emergency'
    assert directory.stat().st_mode & 0o777 == 0o700
    identity = load(root, 'identity.json')
    assert len(identity['key']) == 64 and len(identity['installation_id']) == 36
    nodes = {'__entry__': {'host': '198.51.100.1', 'port': 51821},
             'se': {'host': '203.0.113.1', 'port': 51821},
             'entry': {'host': '203.0.113.2', 'port': 51821}}
    for name, node in (('ru.json', '__entry__'), ('se.json', 'se'), ('entry.json', 'entry')):
        assert (directory / name).stat().st_mode & 0o777 == 0o600
        assert load(root, name) == dict(identity, node=node, entry_id='__entry__', nodes=nodes)


def test_repeat_keeps_the_identity_and_follows_the_exit_list(tmp_path):
    root = root_with(tmp_path, config(['se']))
    provision.provision(root)
    identity = load(root, 'identity.json')
    provision.provision(root)
    assert load(root, 'identity.json') == identity
    root_with(tmp_path, config(['se', 'fi']))
    provision.provision(root)
    assert load(root, 'fi.json')['key'] == identity['key']
    assert sorted(load(root, 'se.json')['nodes']) == ['__entry__', 'fi', 'se']
    assert not [name for name in files(root) if name.endswith('.new')]


def test_loose_directory_mode_is_tightened(tmp_path):
    root = root_with(tmp_path, config(['se']))
    directory = root / 'infra/secrets/emergency'
    directory.mkdir(parents=True, mode=0o755)
    directory.chmod(0o755)
    provision.provision(root)
    assert directory.stat().st_mode & 0o777 == 0o700


def test_entry_alone_is_a_valid_membership(tmp_path):
    root = root_with(tmp_path, config([]))
    provision.provision(root)
    assert files(root) == ['.lock', 'identity.json', 'ru.json']
    assert list(load(root, 'ru.json')['nodes']) == ['__entry__']


@pytest.mark.parametrize('tags', [['se', 'se'], [f'x{n}' for n in range(65)]])
def test_duplicate_tag_or_too_many_nodes_is_refused_before_node_files(tmp_path, tags):
    root = root_with(tmp_path, config(tags))
    with pytest.raises(ValueError, match='invalid observer membership'):
        provision.provision(root)
    assert files(root) == ['.lock', 'identity.json']


def test_largest_allowed_membership_passes(tmp_path):
    root = root_with(tmp_path, config([f'x{n}' for n in range(64)]))
    provision.provision(root)
    assert len(files(root)) == 64 + 3


@pytest.mark.parametrize('text', ['', 'entry: {host: 198.51.100.1}\n', 'exits: []\n', 'entry: [\n'])
def test_empty_or_broken_config_writes_no_node_config(tmp_path, text):
    root = root_with(tmp_path, text)
    with pytest.raises(Exception) as caught:  # noqa: PT011 - класс зависит от вида поломки
        provision.provision(root)
    assert not isinstance(caught.value, AssertionError)
    directory = root / 'infra/secrets/emergency'
    assert not directory.exists() or 'ru.json' not in os.listdir(directory)


def test_missing_config_is_an_error_that_names_the_file(tmp_path):
    with pytest.raises(FileNotFoundError, match='config.yaml'):
        provision.provision(tmp_path)
    assert os.listdir(tmp_path) == []


def test_broken_identity_is_not_silently_replaced(tmp_path):
    root = root_with(tmp_path, config(['se']))
    directory = root / 'infra/secrets/emergency'
    directory.mkdir(parents=True)
    (directory / 'identity.json').write_text('{half')
    with pytest.raises(json.JSONDecodeError):
        provision.provision(root)
    assert (directory / 'identity.json').read_text() == '{half'
