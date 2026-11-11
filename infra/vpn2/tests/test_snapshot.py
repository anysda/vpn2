import copy
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import snapshot
from snapshot import (
    SnapshotError,
    diff,
    dump,
    load,
    load_ignore,
    subtract,
    unused_ignore,
)

TOOLS = str(Path(snapshot.__file__).resolve().parent)


def file_record(sha, mode='0644'):
    return {'type': 'file', 'mode': mode, 'owner': 'root:root', 'sha256': sha}


def make():
    return {
        'schema': 1,
        'files': {
            '/etc/ufw/ufw.conf': file_record('aa'),
            '/etc/wireguard/wg0.conf': file_record('bb', '0600'),
        },
        'units': {'xray.service': {'active': 'active', 'enabled': True}},
    }


def rule(name, field='*', section='files', reason='because of testing'):
    return {'section': section, 'name': name, 'field': field, 'reason': reason}


def write_ignore(tmp_path, text):
    path = tmp_path / 'ignore.yml'
    path.write_text(text, encoding='utf-8')
    return path


def test_equal_snapshots_give_no_lines():
    assert diff(make(), make()) == []


def test_changed_field_is_one_line_with_both_values():
    new = make()
    new['units']['xray.service']['active'] = 'failed'
    assert diff(make(), new) == ['units xray.service active: "active" -> "failed"']


def test_missing_extra_and_new_field_lines():
    new = make()
    del new['files']['/etc/ufw/ufw.conf']
    new['files']['/etc/new'] = file_record('cc')
    new['units']['xray.service']['since'] = 'today'
    assert diff(make(), new) == [
        'files /etc/new: лишнее в текущем',
        'files /etc/ufw/ufw.conf: нет в текущем',
        'units xray.service since: нет -> "today"',
    ]


def test_field_gone_from_current_and_non_ascii_values():
    old = {'schema': 1, 'files': {'/a': {'note': 'привет', 'mode': '0644'}}}
    new = {'schema': 1, 'files': {'/a': {'mode': '0644'}}}
    assert diff(old, new) == ['files /a note: "привет" -> нет']


def test_lines_are_sorted():
    new = make()
    new['units']['xray.service']['active'] = 'failed'
    new['files']['/etc/ufw/ufw.conf']['sha256'] = 'zz'
    lines = diff(make(), new)
    assert len(lines) == 2
    assert lines == sorted(lines)


def test_schema_mismatch_is_the_only_line():
    new = make()
    new['schema'] = 2
    new['units']['xray.service']['active'] = 'failed'
    assert diff(make(), new) == ['schema: 1 -> 2']


def test_positive_control_sha256_change_is_caught_under_ignore():
    new = copy.deepcopy(make())
    new['files']['/etc/ufw/ufw.conf']['sha256'] = 'changed'
    ignore = [rule('/etc/wireguard/*.conf')]
    assert diff(make(), new, ignore) == ['files /etc/ufw/ufw.conf sha256: "aa" -> "changed"']
    assert diff(make(), new, [rule('/etc/ufw/ufw.conf', 'mode')]) == [
        'files /etc/ufw/ufw.conf sha256: "aa" -> "changed"']


def test_field_ignore_hides_only_that_field_on_both_sides():
    new = make()
    new['files']['/etc/ufw/ufw.conf']['mode'] = '0600'
    new['files']['/etc/ufw/ufw.conf']['sha256'] = 'changed'
    ignore = [rule('/etc/ufw/ufw.conf', 'mode')]
    assert diff(make(), new, ignore) == ['files /etc/ufw/ufw.conf sha256: "aa" -> "changed"']


def env_pair(old_env, new_env):
    old, new = make(), make()
    old['units']['xray.service']['env'] = old_env
    new['units']['xray.service']['env'] = new_env
    return old, new


def env_rule(field='env:A'):
    return rule('xray.service', field, section='units')


def test_env_key_ignore_hides_only_that_variable():
    old, new = env_pair(['A=1', 'B=2'], ['A=9', 'B=2'])
    assert diff(old, new, [env_rule()]) == []
    assert old['units']['xray.service']['env'] == ['A=1', 'B=2']


def test_env_key_ignore_keeps_other_variable_difference():
    old, new = env_pair(['A=1', 'B=2'], ['A=9', 'B=3'])
    assert diff(old, new, [env_rule()]) == ['units xray.service env: ["B=2"] -> ["B=3"]']


def test_env_key_ignore_unused_when_no_such_variable():
    old, new = env_pair(['B=2'], ['B=2'])
    assert unused_ignore(old, new, [env_rule()]) == [env_rule()]
    old, new = env_pair(['B=2'], ['A=1', 'B=2'])
    assert unused_ignore(old, new, [env_rule()]) == []


def test_env_key_ignore_variable_on_one_side_only():
    old, new = env_pair(['B=2'], ['A=1', 'B=2'])
    assert diff(old, new, [env_rule()]) == []


def test_star_field_hides_missing_and_extra():
    new = make()
    del new['files']['/etc/ufw/ufw.conf']
    new['files']['/etc/wireguard/extra.conf'] = file_record('cc')
    ignore = [rule('/etc/ufw/*'), rule('/etc/wireguard/extra.conf')]
    assert diff(make(), new, ignore) == []
    assert diff(new, make(), ignore) == []


def test_ignore_is_scoped_by_section():
    new = make()
    new['units']['xray.service']['active'] = 'failed'
    assert diff(make(), new, [rule('xray.service', section='files')]) != []
    assert diff(make(), new, [rule('xray.service', section='units')]) == []


def test_mask_star_crosses_slash():
    old = {'schema': 1, 'files': {'/etc/wireguard/sub/x.conf': file_record('aa')}}
    new = {'schema': 1, 'files': {'/etc/wireguard/sub/x.conf': file_record('bb')}}
    assert diff(old, new, [rule('/etc/wireguard/*.conf')]) == []


def test_load_ignore_reads_records(tmp_path):
    path = write_ignore(tmp_path, '- {section: files, name: "/x*", field: "*", reason: long enough reason}\n')
    assert load_ignore(path) == [rule('/x*', reason='long enough reason')]


def test_load_ignore_empty_file(tmp_path):
    assert load_ignore(write_ignore(tmp_path, '')) == []


@pytest.mark.parametrize('text, needle', [
    ('- {section: files, name: a, field: "*"}\n', 'record 1'),
    ('- {section: files, name: a, field: "*", reason: short}\n', 'record 1'),
    ('- {section: files, name: a, field: "*", reason: long enough reason, extra: 1}\n', 'record 1'),
    ('- {section: files, name: a, field: "*", reason: long enough reason}\n'
     '- {section: files, name: b, field: "*"}\n', 'record 2'),
    ('- just text\n', 'record 1'),
    ('section: files\n', 'list'),
    ('- [unclosed\n', 'ignore.yml'),
])
def test_load_ignore_rejects_bad_input(tmp_path, text, needle):
    with pytest.raises(SnapshotError, match=needle):
        load_ignore(write_ignore(tmp_path, text))


def test_subtract_semantics():
    base = make()
    snap = make()
    snap['files']['/etc/wireguard/wg0.conf']['sha256'] = 'changed'
    del snap['files']['/etc/ufw/ufw.conf']
    snap['ports'] = {'443/tcp': {'state': 'listen'}}
    before = copy.deepcopy(snap)
    result = subtract(snap, base)
    assert result == {
        'schema': 1,
        'files': {
            '/etc/wireguard/wg0.conf': file_record('changed', '0600'),
            '/etc/ufw/ufw.conf': {'absent': True},
        },
        'ports': {'443/tcp': {'state': 'listen'}},
    }
    assert 'units' not in result
    assert snap == before
    assert base == make()
    assert diff(result, result) == []
    assert subtract(make(), make()) == {'schema': 1}


def test_unused_ignore_returns_exactly_unmatched():
    used_star = rule('/etc/ufw/*')
    used_field = rule('/etc/wireguard/wg0.conf', 'mode')
    stale_name = rule('/nowhere')
    stale_field = rule('/etc/ufw/ufw.conf', 'nosuchfield')
    stale_section = rule('xray.service', section='files')
    only_new = rule('/etc/only-new')
    new = make()
    new['files']['/etc/only-new'] = file_record('cc')
    ignore = [used_star, used_field, stale_name, stale_field, stale_section, only_new]
    assert unused_ignore(make(), new, ignore) == [stale_name, stale_field, stale_section]
    assert unused_ignore(make(), make(), ignore)[-1] == only_new


@pytest.mark.parametrize('name', ['snap.json', 'snap.json.gz'])
def test_dump_load_round_trip(tmp_path, name):
    path = tmp_path / name
    data = make()
    data['files']['/etc/привет'] = file_record('dd')
    dump(data, path)
    assert load(path) == data
    assert sorted(p.name for p in tmp_path.iterdir()) == [name]


def test_dump_is_sorted_json_and_gz_bytes_are_stable(tmp_path):
    plain = tmp_path / 'a.json'
    dump(make(), plain)
    text = plain.read_text(encoding='utf-8')
    assert text.index('"files"') < text.index('"schema"') < text.index('"units"')
    assert '\n "files"' in text
    first, second = tmp_path / 'one.json.gz', tmp_path / 'two.json.gz'
    dump(make(), first)
    dump(make(), second)
    assert first.read_bytes() == second.read_bytes()


def test_import_works_without_yaml():
    code = ("import sys; sys.modules['yaml'] = None; "
            "import snapshot as s; "
            "assert s.diff({'schema': 1}, {'schema': 1}) == []")
    done = subprocess.run([sys.executable, '-c', code], env={'PYTHONPATH': TOOLS},
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
