import os

import pytest

import vpn2.cli as cli
from vpn2.control import apply, server


@pytest.mark.parametrize('argv, code', [([], 2), (['nope'], 2), (['--help'], 0)])
def test_usage(argv, code, capsys):
    assert cli.main(['control', *argv]) == code
    out = capsys.readouterr()
    assert 'serve' in (out.out if code == 0 else out.err)


def test_serve_passes_the_flags_to_the_server(monkeypatch):
    seen = []
    monkeypatch.setattr(server, 'main', seen.append)
    assert cli.main(['control', 'serve', '--probe']) == 0
    assert seen == [['--probe']]


def test_apply_runs_under_a_private_umask(monkeypatch):
    seen = []

    def fake(argv):
        mask = os.umask(0o022)
        os.umask(mask)
        seen.append((argv, mask))

    monkeypatch.setattr(apply, 'main', fake)
    old = os.umask(0o022)
    try:
        assert cli.main(['control', 'apply', 'resume', 'wg']) == 0
    finally:
        os.umask(old)
    assert seen == [(['resume', 'wg'], 0o077)]


def test_apply_refuses_an_unknown_resource(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(['control', 'apply', 'install', 'nope', '/dev/null'])
    assert stop.value.code == 2
    assert 'nope' in capsys.readouterr().err
