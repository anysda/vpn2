import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import snapshot

TOOLS = str(Path(__file__).resolve().parents[2] / 'tools')
SECTIONS = {'files', 'units', 'ports', 'firewall', 'containers', 'wg', 'verify'}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, '_run', lambda *_: (127, ''))
    (tmp_path / 'root' / 'etc').mkdir(parents=True)
    (tmp_path / 'root' / 'etc' / 'app.conf').write_text('a=1\n')
    return tmp_path / 'root'


def take(root, out):
    assert snapshot.main(['--root', str(root), '--out', str(out)]) == 0


def corrupt(root):
    with open(root / 'etc' / 'app.conf', 'a') as file:
        file.write('x')


def test_snapshot_writes_file_and_diff_equal(root, tmp_path, capsys):
    take(root, tmp_path / 'a.json')
    data = json.loads((tmp_path / 'a.json').read_text())
    assert data['schema'] == 1 and SECTIONS <= data.keys()
    take(root, tmp_path / 'b.json')
    capsys.readouterr()
    assert snapshot.main(['diff', str(tmp_path / 'a.json'), str(tmp_path / 'b.json')]) == 0
    assert capsys.readouterr().out.strip() == 'слепки совпадают'


def test_snapshot_to_stdout(root, capsys):
    assert snapshot.main(['--root', str(root)]) == 0
    assert json.loads(capsys.readouterr().out)['schema'] == 1


def test_corruption_is_caught(root, tmp_path, capsys):
    take(root, tmp_path / 'a.json')
    corrupt(root)
    take(root, tmp_path / 'b.json')
    capsys.readouterr()
    assert snapshot.main(['diff', str(tmp_path / 'a.json'), str(tmp_path / 'b.json')]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert '/etc/app.conf' in lines[0] and 'sha256' in lines[0]
    assert lines[-1] == 'расхождений: 1'


def test_baseline_keeps_only_corrupted(root, tmp_path):
    take(root, tmp_path / 'a.json')
    corrupt(root)
    out = tmp_path / 'delta.json'
    assert snapshot.main(['--root', str(root), '--baseline', str(tmp_path / 'a.json'),
                     '--out', str(out)]) == 0
    assert list(json.loads(out.read_text())['files']) == ['/etc/app.conf']


def test_gz_baseline_is_read(root, tmp_path, capsys):
    snapshot.dump(snapshot.collect(str(root)), tmp_path / 'a.json.gz')
    take(root, tmp_path / 'a.json')
    capsys.readouterr()
    assert snapshot.main(['diff', str(tmp_path / 'a.json.gz'), str(tmp_path / 'a.json')]) == 0


def write_ignore(tmp_path, name, field='sha256'):
    path = tmp_path / 'ignore.yml'
    path.write_text(f'- section: files\n  name: {name}\n  field: {field}\n  reason: проверка команды сверки\n')
    return path


def corrupted_pair(root, tmp_path):
    take(root, tmp_path / 'a.json')
    corrupt(root)
    take(root, tmp_path / 'b.json')
    return str(tmp_path / 'a.json'), str(tmp_path / 'b.json')


def test_ignore_rule_closes_difference(root, tmp_path, capsys):
    old, new = corrupted_pair(root, tmp_path)
    rules = write_ignore(tmp_path, '/etc/app.conf')
    capsys.readouterr()
    assert snapshot.main(['diff', '--ignore', str(rules), old, new]) == 0
    assert 'ignore не сработал' not in capsys.readouterr().err


def test_unused_ignore_reported_code_stays(root, tmp_path, capsys):
    old, new = corrupted_pair(root, tmp_path)
    rules = write_ignore(tmp_path, '/etc/none.conf')
    capsys.readouterr()
    assert snapshot.main(['diff', '--ignore', str(rules), old, new]) == 1
    assert 'ignore не сработал: files /etc/none.conf sha256' in capsys.readouterr().err


def one_error_line(capsys):
    err = capsys.readouterr().err
    assert err.startswith('vpn2 snapshot: ') and len(err.splitlines()) == 1, err


def test_missing_baseline_file(tmp_path, capsys):
    assert snapshot.main(['diff', str(tmp_path / 'no.json'), str(tmp_path / 'no.json')]) == 2
    one_error_line(capsys)


def test_baseline_not_json(tmp_path, capsys):
    bad = tmp_path / 'bad.json'
    bad.write_text('не json')
    assert snapshot.main(['diff', str(bad), str(bad)]) == 2
    one_error_line(capsys)


def test_ignore_without_reason(root, tmp_path, capsys):
    take(root, tmp_path / 'a.json')
    rules = tmp_path / 'ignore.yml'
    rules.write_text('- section: files\n  name: x\n  field: sha256\n')
    capsys.readouterr()
    path = str(tmp_path / 'a.json')
    assert snapshot.main(['diff', '--ignore', str(rules), path, path]) == 2
    one_error_line(capsys)


def test_diff_with_one_path(tmp_path, capsys):
    assert snapshot.main(['diff', str(tmp_path / 'a.json')]) == 2
    one_error_line(capsys)


def test_help_is_zero(capsys):
    assert snapshot.main(['--help']) == 0
    assert 'слепок' in capsys.readouterr().out.lower()


def test_runs_without_yaml(root, tmp_path):
    code = ("import sys, runpy; sys.modules['yaml'] = None; "
            "sys.argv = ['snapshot.py', '--root', sys.argv[1], '--out', sys.argv[2]]; "
            "runpy.run_module('snapshot', run_name='__main__')")
    out = tmp_path / 'sub.json'
    done = subprocess.run([sys.executable, '-B', '-c', code, str(root), str(out)],
                          env={'PYTHONPATH': TOOLS}, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert json.loads(out.read_text())['schema'] == 1
