import os

import pytest

from vpn2.watchdog import warp, yt


@pytest.mark.parametrize("save", [warp.save_state, yt.save])
def test_watchdog_state_is_private_as_under_unit_umask(tmp_path, save):
    """Юниты сторожей WARP и YouTube шли с UMask=0077: состояние 0600."""
    old = os.umask(0o022)
    try:
        save(tmp_path / "state.json", {"pending": []})
    finally:
        os.umask(old)
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
