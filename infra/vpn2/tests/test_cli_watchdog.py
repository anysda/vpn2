import pytest

import vpn2.cli as cli


def test_usage_without_name(capsys):
    assert cli.main(["watchdog"]) == 2
    assert "failover" in capsys.readouterr().err


def test_unknown_name(capsys):
    assert cli.main(["watchdog", "nope"]) == 2
    assert "Использование" in capsys.readouterr().err


@pytest.mark.parametrize("target", [*cli.WATCHDOGS.values(), *cli.ROUTER.values(), *dict(cli.COMMANDS).values()])
def test_every_table_row_resolves(target):
    assert callable(cli._resolve(target))


def test_arguments_reach_the_watchdog(monkeypatch):
    from vpn2.watchdog import warp
    seen = []
    monkeypatch.setattr(warp, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["watchdog", "warp", "--config", "x"]) == 0
    assert seen == [["--config", "x"]]
