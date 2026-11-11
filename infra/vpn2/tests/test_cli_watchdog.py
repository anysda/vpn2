import pytest

import vpn2.cli as cli
from vpn2.cli import watchdog


def test_usage_without_name(capsys):
    assert cli.main(["watchdog"]) == 2
    assert "failover" in capsys.readouterr().err


def test_unknown_name(capsys):
    assert cli.main(["watchdog", "nope"]) == 2
    assert "Использование" in capsys.readouterr().err


@pytest.mark.parametrize("kind", sorted(watchdog.KINDS))
def test_every_kind_resolves(kind):
    module, entry = watchdog.KINDS[kind]
    loaded = __import__(f"vpn2.watchdog.{module}", fromlist=[entry])
    assert callable(getattr(loaded, entry))


def test_arguments_reach_the_watchdog(monkeypatch):
    from vpn2.watchdog import warp
    seen = []
    monkeypatch.setattr(warp, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["watchdog", "warp", "--config", "x"]) == 0
    assert seen == [["--config", "x"]]
