"""Перезапуск службы по отпечатку входов: подменный systemctl на PATH помнит вызовы и состояние службы."""
import hashlib
import subprocess
import sys

import pytest

from vpn2 import svc

SYSTEMCTL = r'''#!{python}
import sys
box = {box!r}
args = [arg for arg in sys.argv[1:] if not arg.startswith('--')]
open(box + '/calls', 'a').write(' '.join(args) + '\n')
state = open(box + '/state').read().strip()
if args[0] == 'is-active':
    sys.exit(0 if state == 'active' else 3)
if args[0] == 'restart':
    if state == 'broken':
        sys.stderr.write('Job for unit failed\n')
        sys.exit(1)
    open(box + '/state', 'w').write('active')
'''


class Box:
    def __init__(self, path):
        self.path = path
        (path / 'calls').write_text('')
        self.state('inactive')

    def state(self, value):
        (self.path / 'state').write_text(value)

    def restarts(self):
        calls = (self.path / 'calls').read_text().splitlines()
        (self.path / 'calls').write_text('')
        return [call for call in calls if call.startswith('restart')]


@pytest.fixture
def box(tmp_path, monkeypatch):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'systemctl').write_text(SYSTEMCTL.format(python=sys.executable, box=str(tmp_path)))
    (bin_dir / 'systemctl').chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir))
    return Box(tmp_path)


@pytest.fixture
def inputs(tmp_path):
    config = tmp_path / 'unit.conf'
    config.write_text('port 1\n')
    return [config, tmp_path / 'absent.bin']


def test_first_run_restarts_and_the_repeat_does_not(box, inputs, tmp_path, capsys):
    applied = tmp_path / 'var/applied'
    assert svc.restart_if_changed('demo', inputs, applied) is True
    assert box.restarts() == ['restart demo']
    stored = applied / 'demo.sha256'
    assert stored.read_text() == hashlib.sha256(b'port 1\n').hexdigest() + '\n'
    assert stored.stat().st_mode & 0o777 == 0o644
    for _ in range(2):
        assert svc.restart_if_changed('demo', inputs, applied) is False
    assert box.restarts() == []
    assert capsys.readouterr().out == ('demo перезапущен\n'
                                       + 'demo: конфиг и бинарник прежние - не перезапускаю\n' * 2)


def test_changed_or_appeared_input_restarts(box, inputs, tmp_path):
    applied = tmp_path / 'applied'
    svc.restart_if_changed('demo', inputs, applied)
    inputs[0].write_text('port 2\n')
    assert svc.restart_if_changed('demo', inputs, applied) is True
    inputs[1].write_bytes(b'\x00binary')
    assert svc.restart_if_changed('demo', inputs, applied) is True
    assert svc.restart_if_changed('demo', inputs, applied) is False
    assert box.restarts() == ['restart demo'] * 3


def test_stopped_service_is_started_even_with_the_same_inputs(box, inputs, tmp_path):
    applied = tmp_path / 'applied'
    svc.restart_if_changed('demo', inputs, applied)
    box.state('inactive')
    assert svc.restart_if_changed('demo', inputs, applied) is True
    assert svc.active('demo') is True


def test_forget_forces_one_restart_and_repeats_quietly(box, inputs, tmp_path):
    applied = tmp_path / 'applied'
    svc.restart_if_changed('demo', inputs, applied)
    box.restarts()
    svc.forget('demo', applied)
    svc.forget('demo', applied)
    svc.forget('other', tmp_path / 'no-such-dir')
    assert svc.restart_if_changed('demo', inputs, applied) is True
    assert svc.restart_if_changed('demo', inputs, applied) is False
    assert box.restarts() == ['restart demo']


def test_units_keep_separate_fingerprints(box, inputs, tmp_path):
    applied = tmp_path / 'applied'
    svc.restart_if_changed('one', inputs, applied)
    assert svc.restart_if_changed('two', inputs, applied) is True
    assert sorted(path.name for path in applied.iterdir()) == ['one.sha256', 'two.sha256']
    assert box.restarts() == ['restart one', 'restart two']


def test_failed_restart_is_an_error_and_leaves_no_fingerprint(box, inputs, tmp_path):
    applied = tmp_path / 'applied'
    box.state('broken')
    for _ in range(2):
        with pytest.raises(subprocess.CalledProcessError):
            svc.restart_if_changed('demo', inputs, applied)
    assert not applied.exists()
    box.state('inactive')
    assert svc.restart_if_changed('demo', inputs, applied) is True


def test_broken_stored_fingerprint_restarts_and_is_rewritten(box, inputs, tmp_path):
    applied = tmp_path / 'applied'
    svc.restart_if_changed('demo', inputs, applied)
    box.restarts()
    for body in ('', 'garbage\n'):
        (applied / 'demo.sha256').write_text(body)
        assert svc.restart_if_changed('demo', inputs, applied) is True
        assert svc.restart_if_changed('demo', inputs, applied) is False
    assert box.restarts() == ['restart demo'] * 2


def test_fingerprint_of_nothing_is_the_empty_hash(tmp_path):
    empty = hashlib.sha256().hexdigest()
    assert svc.fingerprint([]) == empty
    assert svc.fingerprint([tmp_path / 'absent', tmp_path]) == empty
