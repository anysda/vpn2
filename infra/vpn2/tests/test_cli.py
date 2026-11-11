import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import vpn2.cli as cli

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_no_args_and_unknown_command(capsys):
    assert cli.main([]) == 2
    assert "version" in capsys.readouterr().err
    assert cli.main(["nonexistent"]) == 2
    err = capsys.readouterr().err
    assert "nonexistent" in err
    assert "version" in err


def test_help_lists_commands_on_stdout(capsys):
    assert cli.main(["--help"]) == 0
    assert "version" in capsys.readouterr().out


def test_version_matches_pyproject():
    want = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    res = subprocess.run(
        [sys.executable, "-m", "vpn2.cli", "version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout == want + "\n"


@pytest.mark.parametrize("failure", [RuntimeError("boom"), ImportError("no module")])
def test_failing_command_is_code_1_and_names_itself(failure, monkeypatch, capsys):
    def broken(argv):
        raise failure
    monkeypatch.setattr(cli, "version", broken)
    assert cli.main(["version"]) == 1
    assert "vpn2 version" in capsys.readouterr().err
    assert cli.main(["--help"]) == 0
