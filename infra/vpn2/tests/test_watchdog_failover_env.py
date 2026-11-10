import os

import pytest

from vpn2.watchdog import failover_env


def values(text):
    return dict(line.split("=", 1) for line in text.splitlines())


def test_defaults_and_deploy_owned_head():
    text = failover_env.render("", "sec", {})
    assert text.splitlines()[:4] == [
        "CLASH_API=http://10.99.0.1:9090",
        "CLASH_SECRET=sec",
        "WATCH_GROUP=foreign-best",
        "LANE_STATE_FILE=/var/lib/anysda-failover-watchdog/lanes.json",
    ]
    assert [line.split("=")[0] for line in text.splitlines()[4:]] == [name for name, _, _ in failover_env.SETTINGS]
    assert values(text)["INTERVAL"] == "2"
    assert values(text)["BALANCE"] == "on"


def test_manual_edits_survive_and_deploy_keys_do_not():
    previous = "CLASH_SECRET=old\nWATCH_GROUP=other\nINTERVAL=5\nBALANCE=off\nTG_GROUP=my-group_1\nCONFIRM_GAP=1.25\n"
    got = values(failover_env.render(previous, "new", {"MGMT_IP": "10.1.2.3"}))
    assert got["CLASH_API"] == "http://10.1.2.3:9090"
    assert (got["CLASH_SECRET"], got["WATCH_GROUP"]) == ("new", "foreign-best")
    assert (got["INTERVAL"], got["BALANCE"], got["TG_GROUP"], got["CONFIRM_GAP"]) == ("5", "off", "my-group_1", "1.25")


@pytest.mark.parametrize("line, key, default", [
    ("INTERVAL=abc", "INTERVAL", "2"),
    ("PROBE_URL=ftp://x", "PROBE_URL", "http://www.gstatic.com/generate_204"),
    ("PROBE_URL=http://a b", "PROBE_URL", "http://www.gstatic.com/generate_204"),
    ("BALANCE=maybe", "BALANCE", "on"),
    ("DEAD_AFTER=3.5", "DEAD_AFTER", "3"),
    ("CONFIRM_GAP=.5", "CONFIRM_GAP", "0.4"),
    ("COOLDOWN_S=10 ", "COOLDOWN_S", "60"),
    ("TOLERANCE_MS= 12", "TOLERANCE_MS", "120"),
    ("TG_GROUP=a b", "TG_GROUP", "tg-best"),
    ("XINTERVAL=9", "INTERVAL", "2"),
    ("INTERVAL=٣", "INTERVAL", "2"),
])
def test_broken_value_falls_back_to_default(line, key, default):
    assert values(failover_env.render(line + "\n", "s", {}))[key] == default


def test_last_valid_repeat_wins():
    assert values(failover_env.render("INTERVAL=5\nINTERVAL=7\nINTERVAL=x\n", "s", {}))["INTERVAL"] == "7"


def test_restart_settings_come_only_from_config():
    previous = "RESTART_AFTER_CHECKS=9\nRESTART_MIN_INTERVAL_S=1\n"
    got = values(failover_env.render(previous, "s", {}))
    assert (got["RESTART_AFTER_CHECKS"], got["RESTART_MIN_INTERVAL_S"]) == ("3", "600")
    got = values(failover_env.render(previous, "s", {"RESTART_AFTER_CHECKS": "0", "RESTART_MIN_INTERVAL_S": "900"}))
    assert (got["RESTART_AFTER_CHECKS"], got["RESTART_MIN_INTERVAL_S"]) == ("0", "900")
    assert values(failover_env.render("", "s", {"RESTART_AFTER_CHECKS": ""}))["RESTART_AFTER_CHECKS"] == "3"


def run(tmp_path, capsys):
    code = failover_env.main(["--env-file", str(tmp_path / "w.env"), "--secret-file", str(tmp_path / "secret")])
    return code, capsys.readouterr()


def test_main_writes_once_with_private_mode(tmp_path, capsys):
    (tmp_path / "secret").write_text("sec\n")
    old = os.umask(0o022)
    try:
        code, output = run(tmp_path, capsys)
    finally:
        os.umask(old)
    assert (code, output.out) == (0, "changed=1\n")
    env = tmp_path / "w.env"
    assert env.stat().st_mode & 0o777 == 0o600
    assert "CLASH_SECRET=sec\n" in env.read_text()
    env.chmod(0o644)
    assert run(tmp_path, capsys)[1].out == "changed=0\n"
    assert env.stat().st_mode & 0o777 == 0o600
    env.write_text(env.read_text().replace("INTERVAL=2\n", "INTERVAL=5\n"))
    assert run(tmp_path, capsys)[1].out == "changed=0\n"
    assert "\nINTERVAL=5\n" in env.read_text()


@pytest.mark.parametrize("secret", [None, "", "\n"])
def test_main_refuses_without_secret(tmp_path, capsys, secret):
    if secret is not None:
        (tmp_path / "secret").write_text(secret)
    code, output = run(tmp_path, capsys)
    assert code == 1
    assert "20-ru-router" in output.err
    assert not (tmp_path / "w.env").exists()
