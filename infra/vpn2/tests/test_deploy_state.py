"""Перенос состояния деплоя раскладки v1.1.5 (infra/state, infra/.known_hosts) в каталог состояния."""
import shutil

import pytest

from vpn2.deploy import migrate_state


@pytest.fixture
def layout(tmp_path):
    infra, state = tmp_path / 'infra', tmp_path / 'state'
    (infra / 'state').mkdir(parents=True)
    state.mkdir()
    (infra / '.known_hosts').write_text('host key\n')
    (infra / '.known_hosts.old').write_text('old key\n')
    (infra / 'state/mgmt-ips.txt').write_text('us 2\n')
    (infra / 'state/.hidden').write_text('dot\n')
    (infra / 'state/deploy.lock').touch()
    return infra, state


def test_moves_everything_and_repeats_cleanly(layout):
    infra, state = layout
    migrate_state(infra, state)
    assert sorted(path.name for path in state.iterdir()) == ['.hidden', 'known_hosts', 'known_hosts.old', 'mgmt-ips.txt']
    assert (state / 'known_hosts').stat().st_mode & 0o777 == 0o600
    assert not (infra / 'state').exists() and not (infra / '.known_hosts').exists()
    migrate_state(infra, state)
    assert (state / 'mgmt-ips.txt').read_text() == 'us 2\n'


def test_no_old_layout_is_a_no_op(tmp_path):
    (tmp_path / 'infra').mkdir()
    (tmp_path / 'state').mkdir()
    migrate_state(tmp_path / 'infra', tmp_path / 'state')
    assert list((tmp_path / 'state').iterdir()) == []


def test_interrupted_move_is_finished(layout):
    infra, state = layout
    # Обрыв после копии: цель есть и равна, источник ещё лежит; брошенная копия .tmp перезаписывается.
    shutil.copy2(infra / 'state/mgmt-ips.txt', state / 'mgmt-ips.txt')
    (state / 'known_hosts.tmp').write_text('half')
    migrate_state(infra, state)
    assert (state / 'known_hosts').read_text() == 'host key\n' and not (state / 'known_hosts.tmp').exists()
    assert not (infra / 'state').exists() and not (state / 'checkout-old').exists()


def test_failed_copy_keeps_source(layout):
    infra, state = layout
    (state / 'mgmt-ips.txt').mkdir()  # цель занята каталогом: перенос падает
    with pytest.raises(OSError):
        migrate_state(infra, state)
    assert (infra / 'state/mgmt-ips.txt').read_text() == 'us 2\n'


def test_different_target_wins_and_source_is_kept(layout, capsys):
    infra, state = layout
    (state / 'mgmt-ips.txt').write_text('us 3\n')
    migrate_state(infra, state)
    assert (state / 'mgmt-ips.txt').read_text() == 'us 3\n'
    assert (state / 'checkout-old/mgmt-ips.txt').read_text() == 'us 2\n'
    assert 'состояние в checkout отличалось' in capsys.readouterr().err
