import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import vpn2.cli as cli

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


@pytest.fixture
def extra(tmp_path, monkeypatch):
    """Временный каталог, добавленный в пакет cli."""
    monkeypatch.setattr(cli, "__path__", [*cli.__path__, str(tmp_path)])
    return tmp_path


def write(extra, name, text):
    (extra / f"{name}.py").write_text(text)


def test_new_command_without_editing_main(extra, capsys):
    write(extra, "hello", "def main(argv):\n    print(argv)\n    return 7\n")
    assert cli.main(["hello", "a"]) == 7
    assert capsys.readouterr().out.strip() == "['a']"


def test_none_return_is_zero(extra):
    write(extra, "quiet", "def main(argv):\n    return None\n")
    assert cli.main(["quiet"]) == 0


def test_underscore_in_name_becomes_dash(extra):
    write(extra, "backup_list", "def main(argv):\n    return 3\n")
    assert cli.main(["backup-list"]) == 3


def test_not_commands_hidden(extra, capsys):
    write(extra, "nomain", "X = 1\n")
    write(extra, "_private", "def main(argv):\n    return 0\n")
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "version" in out
    assert "nomain" not in out
    assert "_private" not in out
    assert cli.main(["_private"]) == 2
    assert cli.main(["nomain"]) == 2


def test_broken_module_does_not_break_neighbours(extra, capsys):
    write(extra, "broken", "raise RuntimeError('boom')\n")
    assert cli.main(["version"]) == 0
    capsys.readouterr()
    assert cli.main(["--help"]) == 0
    assert "version" in capsys.readouterr().out
    assert cli.main(["broken"]) == 1
    assert "broken" in capsys.readouterr().err


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


def test_module_raising_system_exit_does_not_break_neighbours(extra, capsys):
    write(extra, "quitter", "raise SystemExit(3)\n")
    assert cli.main(["version"]) == 0
    capsys.readouterr()
    assert cli.main(["--help"]) == 0
    assert "version" in capsys.readouterr().out
    assert cli.main(["quitter"]) == 1
    assert "quitter" in capsys.readouterr().err
