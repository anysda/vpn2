import pytest

import vpn2.cli as cli
from vpn2.cli import watchdog


def test_usage_without_name(capsys):
    assert cli.main(["watchdog"]) == 2
    assert "failover" in capsys.readouterr().err


def test_unknown_name(capsys):
    assert cli.main(["watchdog", "nope"]) == 2
    assert "Использование" in capsys.readouterr().err


def test_hy2_prints_fragment(monkeypatch, capsys):
    monkeypatch.setenv("HY2_UP_MBPS", "10")
    monkeypatch.setenv("HY2_DOWN_MBPS", "20")
    assert cli.main(["watchdog", "hy2"]) == 0
    assert capsys.readouterr().out.strip() == '"up_mbps": 10, "down_mbps": 20,'


def test_hy2_rejects_half_pair(monkeypatch):
    monkeypatch.setenv("HY2_UP_MBPS", "10")
    monkeypatch.delenv("HY2_DOWN_MBPS", raising=False)
    with pytest.raises(SystemExit) as error:
        cli.main(["watchdog", "hy2"])
    assert "нужны обе скорости" in str(error.value)


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
