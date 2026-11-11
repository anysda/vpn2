"""Шаги узла из ролей: числа и строки сверены со стадией 00-bootstrap.sh и anysda-svc.sh."""
import hashlib
import subprocess
import types

import pytest

from vpn2 import apt, swap
from vpn2.cli import host

MIN_FREE = 2147483648


@pytest.mark.parametrize('mem_kb, avail, size, text', [
    (2014000, 20000000000, 2062336000, 'swap: /swapfile 1966 МиБ (RAM 1966 МиБ; свободно 19073 МиБ)'),
    (961000, 20000000000, 984064000, 'swap: /swapfile 938 МиБ (RAM 938 МиБ; свободно 19073 МиБ)'),
    (2014000, 4000000000, 1314914304,
     'swap: /swapfile 1254 МиБ (урезан, деплою оставлено 2048 + 512; свободно 3814 МиБ)'),
    (2014000, 2900000000, 0,
     'swap пропущен: свободно 2765 МиБ, деплою нужно 2048 + 512, swap меньше 256 МиБ не создаю'),
    (2014000, 1000000000, 0,
     'swap пропущен: свободно 953 МиБ, деплою нужно 2048 + 512, swap меньше 256 МиБ не создаю'),
])
def test_swap_plan_matches_stage(mem_kb, avail, size, text):
    assert swap.plan(mem_kb, avail, MIN_FREE) == (size, text)


def test_min_free_falls_back_without_manifest(tmp_path):
    assert swap.min_free_bytes(tmp_path / 'none.json') == MIN_FREE
    (tmp_path / 'm.json').write_text('{"min_free_bytes": 5}')
    assert swap.min_free_bytes(tmp_path / 'm.json') == 5


@pytest.mark.parametrize('stderr, expected', [
    ('E: Failed to fetch http://a/x.deb  502  Bad Gateway [IP: 1.2.3.4 80]\n', 'http://a/x.deb: 502 Bad Gateway'),
    ('W: Failed to fetch http://a/InRelease  404  Not Found [IP: 1.2.3.4 80]\n'
     'E: Failed to fetch http://b/y  Connection timed out\n', 'http://b/y: Connection timed out'),
    ('E: Failed to fetch http://a/x  429  Too Many [IP: 1 80]\n', 'http://a/x: 429 Too Many'),
    ('E: Unable to locate package x\n', None),
])
def test_fetch_failure_matches_library(stderr, expected):
    assert apt.fetch_failure(stderr) == expected


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_wait_idle_gives_up_once_per_deploy(tmp_path):
    marker = tmp_path / 'gave-up'
    clock, lines = Clock(), []
    busy = lambda: [1]  # noqa: E731
    assert apt.wait_idle(timeout=1200, marker=marker, holders=busy, sleep=clock.sleep,
                         clock=clock, say=lines.append) is False
    assert clock.now == 1200
    assert lines[-1] == 'apt так и не освободился за 1200 с, продолжаю'
    assert apt.wait_idle(timeout=1200, marker=marker, holders=busy, sleep=clock.sleep,
                         clock=clock, say=lines.append) is False
    assert clock.now == 1200, 'второй круг ожидания'
    assert lines[-1] == 'apt всё ещё занят, ожидание уже исчерпано, продолжаю'
    # свободный apt снимает метку: следующий деплой ждёт как обычно
    assert apt.wait_idle(marker=marker, holders=lambda: [], sleep=clock.sleep, clock=clock,
                         say=lines.append) is True
    assert not marker.exists()


def test_wait_idle_reports_release(tmp_path):
    clock, lines = Clock(), []
    answers = iter([[7], [7], []])
    assert apt.wait_idle(marker=tmp_path / 'm', holders=lambda: next(answers), sleep=clock.sleep,
                         clock=clock, say=lines.append) is True
    assert lines[0].startswith('apt занят, жду 7 ') and lines[0].endswith('0 с')
    assert lines[-1] == 'apt освободился через 10 с'


def test_apt_get_short_lock_timeout_after_give_up(tmp_path):
    calls = []

    def run(argv, **kwargs):
        del kwargs
        calls.append(argv)
        return types.SimpleNamespace(returncode=100, stderr='E: Could not get lock\n')

    code = apt.apt_get(['install', 'x'], marker=tmp_path / 'm', wait=lambda **_: False, run=run)
    assert code == 100
    assert calls == [['apt-get', '-o', 'DPkg::Lock::Timeout=60', 'install', 'x']]


def test_apt_get_retries_fetch_failure_three_times(tmp_path):
    calls, pauses = [], []

    def run(argv, **kwargs):
        del kwargs
        calls.append(argv)
        return types.SimpleNamespace(returncode=100, stderr='E: Failed to fetch http://a/x  503  Oops\n')

    code = apt.apt_get(['update'], marker=tmp_path / 'm', wait=lambda **_: True, run=run,
                       sleep=pauses.append, say=lambda _text: None)
    assert (code, len(calls), pauses) == (100, 3, [5, 10])
    assert calls[0][2] == 'DPkg::Lock::Timeout=1200'


def test_lists_are_fresh(tmp_path):
    lists, sources = tmp_path / 'lists', tmp_path / 'sources.list.d'
    lists.mkdir()
    sources.mkdir()
    assert not apt.lists_are_fresh(lists, [sources]), 'нет индекса пакетов'
    (lists / 'archive_dists_noble_main_binary-amd64_Packages.lz4').write_text('x')
    assert apt.lists_are_fresh(lists, [sources])
    stamp = lists.stat().st_mtime
    assert not apt.lists_are_fresh(lists, [sources], now=stamp + 3601)
    (sources / 'docker.list').write_text('deb x')
    import os
    os.utime(sources / 'docker.list', (stamp + 5, stamp + 5))
    assert not apt.lists_are_fresh(lists, [sources]), 'источник новее списков'


def test_docker_source_line():
    text = 'ID=ubuntu\nVERSION_CODENAME="noble"\n'
    assert apt.docker_source_line(text, 'amd64') == ('ubuntu', (
        'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] '
        'https://download.docker.com/linux/ubuntu noble stable'))
    with pytest.raises(ValueError):
        apt.docker_source_line('ID=fedora\nVERSION_CODENAME=x\n', 'amd64')
    with pytest.raises(ValueError):
        apt.docker_source_line('ID=debian\n', 'amd64')


def test_f2b_ignore_matches_stage():
    resolve = lambda _name: ['203.0.113.7', '203.0.113.7', '2001:db8::1']  # noqa: E731
    assert host.f2b_ignore('198.51.100.9 50000 22', 'entry.example', resolve) == [
        '127.0.0.1/8', '::1', '198.51.100.9', '203.0.113.7', '2001:db8::1']
    assert host.f2b_ignore('127.0.0.1 1 22', '198.51.100.9') == ['127.0.0.1/8', '::1', '198.51.100.9']
    assert host.f2b_ignore('', '') == ['127.0.0.1/8', '::1']


def test_fingerprint_is_sha256_of_concatenation(tmp_path):
    (tmp_path / 'a').write_bytes(b'one\n')
    (tmp_path / 'b').write_bytes(b'two')
    paths = [tmp_path / 'a', tmp_path / 'missing', tmp_path / 'b']
    assert host.fingerprint(paths) == hashlib.sha256(b'one\ntwo').hexdigest()


def test_restart_if_changed(tmp_path):
    (tmp_path / 'conf').write_text('1')
    calls = []

    def run(argv, **kwargs):
        del kwargs
        calls.append(argv[1])
        return subprocess.CompletedProcess(argv, 0)

    applied = tmp_path / 'applied'
    assert host.restart_if_changed('x', [tmp_path / 'conf'], applied=applied, run=run) is True
    assert (applied / 'x.sha256').read_text() == hashlib.sha256(b'1').hexdigest() + '\n'
    assert host.restart_if_changed('x', [tmp_path / 'conf'], applied=applied, run=run) is False
    (tmp_path / 'conf').write_text('2')
    assert host.restart_if_changed('x', [tmp_path / 'conf'], applied=applied, run=run) is True
    assert calls.count('restart') == 2
    # Экзит держит отпечаток sing-box в прежнем файле стадии: первый прогон роли
    # на живом узле не рвёт туннели лишним перезапуском.
    record = tmp_path / 'sing-box' / 'applied.sha256'
    record.parent.mkdir()
    record.write_text(hashlib.sha256(b'2').hexdigest() + '\n')
    assert host.restart_if_changed('x', [tmp_path / 'conf'], record=record, run=run) is False
    (tmp_path / 'conf').write_text('3')
    assert host.restart_if_changed('x', [tmp_path / 'conf'], record=record, run=run) is True
    assert record.read_text() == hashlib.sha256(b'3').hexdigest() + '\n'


class Sshd:
    """sshd и systemctl узла: -T отдаёт настройки, -t проверяет конфиг."""

    def __init__(self, *, penalties=True, valid=True, active=True):
        self.penalties, self.valid, self.active, self.calls = penalties, valid, active, []

    def __call__(self, argv, **kwargs):
        del kwargs
        self.calls.append(' '.join(argv))
        if argv == ['sshd', '-T']:
            text = 'port 22\n' + ('persourcepenalties crash:90s\n' if self.penalties else '')
            return subprocess.CompletedProcess(argv, 0, stdout=text)
        if argv == ['sshd', '-t']:
            return subprocess.CompletedProcess(argv, 0 if self.valid else 255)
        if argv[:2] == ['systemctl', 'is-active']:
            return subprocess.CompletedProcess(argv, 0 if self.active else 3)
        return subprocess.CompletedProcess(argv, 0)


def sshd_root(tmp_path):
    (tmp_path / 'etc/ssh').mkdir(parents=True)
    (tmp_path / 'etc/ssh/sshd_config').write_text(
        'Include /etc/ssh/sshd_config.d/*.conf\n#PermitRootLogin prohibit-password\n'
        '#PasswordAuthentication yes\nKbdInteractiveAuthentication yes\nUsePAM yes\n')
    return tmp_path


def test_sshd_matches_stage_files(tmp_path):
    root, run = sshd_root(tmp_path), Sshd()
    assert host.sshd('no', root=root, run=run) is True
    assert (root / 'etc/ssh/sshd_config.d/00-anysda.conf').read_text() == (
        '# anysda-vpn — set by infra/scripts/00-bootstrap.sh\nPasswordAuthentication no\n'
        'PermitRootLogin prohibit-password\nKbdInteractiveAuthentication no\n')
    assert (root / 'etc/ssh/sshd_config').read_text() == (
        'Include /etc/ssh/sshd_config.d/*.conf\nPermitRootLogin prohibit-password\n'
        'PasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM yes\n')
    assert (root / 'etc/ssh/sshd_config.d/99-anysda-no-penalties.conf').read_text() == 'PerSourcePenalties no\n'
    assert (root / 'run/sshd').is_dir()
    assert run.calls[-1] == 'systemctl reload-or-restart ssh'
    # повтор ничего не меняет и sshd не трогает
    run.calls.clear()
    assert host.sshd('no', root=root, run=run) is False
    assert 'systemctl reload-or-restart ssh' not in run.calls


def test_sshd_old_openssh_gets_no_penalties_file(tmp_path):
    root = sshd_root(tmp_path)
    (root / 'etc/ssh/sshd_config.d').mkdir()
    (root / 'etc/ssh/sshd_config.d/99-anysda-no-penalties.conf').write_text('PerSourcePenalties no\n')
    host.sshd('yes', root=root, run=Sshd(penalties=False))
    assert not (root / 'etc/ssh/sshd_config.d/99-anysda-no-penalties.conf').exists()
    assert 'PermitRootLogin yes\n' in (root / 'etc/ssh/sshd_config.d/00-anysda.conf').read_text()


def test_sshd_invalid_config_rolls_back_to_password(tmp_path):
    root, run = sshd_root(tmp_path), Sshd(valid=False)
    with pytest.raises(RuntimeError, match='конфиг sshd невалиден'):
        host.sshd('no', root=root, run=run)
    assert 'PasswordAuthentication yes\n' in (root / 'etc/ssh/sshd_config.d/00-anysda.conf').read_text()
    assert not (root / 'etc/ssh/sshd_config.d/99-anysda-no-penalties.conf').exists()
    assert 'systemctl reload-or-restart ssh' not in run.calls


def test_sshd_stopped_service_is_started(tmp_path):
    root, run = sshd_root(tmp_path), Sshd(active=False)
    host.sshd('no', root=root, run=run)
    run.calls.clear()
    host.sshd('no', root=root, run=run)
    assert run.calls[-1] == 'systemctl reload-or-restart ssh'


def test_nft_preserve_writes_dropin_once(tmp_path):
    (tmp_path / 'etc').mkdir()
    (tmp_path / 'etc/nftables.conf').write_text('#!/usr/sbin/nft -f\nflush ruleset\ntable inet filter {}\n')
    calls = []
    run = lambda argv, **_: calls.append(argv)  # noqa: E731
    assert host.nft_preserve(root=tmp_path, run=run) is True
    assert (tmp_path / 'etc/systemd/system/nftables.service.d/10-anysda-preserve.conf').read_text() == (
        '[Service]\n# Stopping this loader must not remove rules owned by other services.\nExecStop=\n')
    assert 'flush ruleset' not in (tmp_path / 'etc/nftables.conf').read_text().splitlines()
    assert calls == [['systemctl', 'daemon-reload']]
    assert host.nft_preserve(root=tmp_path, run=run) is False
    assert len(calls) == 1


class SwapNode:
    """Узел 2 ГБ без swap: swapon не берёт файл с дырами, пока его не записал dd."""

    def __init__(self, tmp_path, monkeypatch, *, virt='none'):
        self.root, self.calls, self.virt = tmp_path, [], virt
        self.file = tmp_path / 'swapfile'
        for name, text in (('proc/meminfo', 'MemTotal: 2014000 kB\n'), ('proc/swaps', 'Filename Type\n'),
                           ('etc/fstab', '/dev/vda1 / ext4 defaults 0 1\n'), ('etc/sysctl.d/.keep', '')):
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_text(text)
        monkeypatch.setattr(swap, 'SWAPFILE', str(self.file))
        monkeypatch.setattr(swap, '_ok', self.ok)
        monkeypatch.setattr(swap.shutil, 'which', lambda _name: '/usr/bin/systemd-detect-virt')
        monkeypatch.setattr(swap.shutil, 'disk_usage', lambda _path: types.SimpleNamespace(free=20 * 1024 ** 3))
        monkeypatch.setattr(swap.subprocess, 'run', self.run)
        self.written = False

    def run(self, *_, **__):
        return types.SimpleNamespace(stdout=self.virt + '\n', returncode=0)

    def ok(self, argv):
        self.calls.append(argv[0])
        if argv[0] == 'fallocate':
            self.file.write_bytes(b'')
        if argv[0] == 'dd':
            self.file.write_bytes(b'')
            self.written = True
        return argv[0] != 'swapon' or self.written


def test_swap_holey_file_recreated_with_dd(tmp_path, monkeypatch):
    node, lines = SwapNode(tmp_path, monkeypatch), []
    node.file.write_bytes(b'')
    assert swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path) is True
    assert lines == [f'swap: включаю имеющийся {node.file}', f'swap: {node.file} не включается, пересоздаю',
                     f'swap: {node.file} 1966 МиБ (RAM 1966 МиБ; свободно 20480 МиБ)']
    assert node.calls == ['swapon', 'mkswap', 'swapon', 'dd', 'mkswap', 'swapon']
    assert (tmp_path / 'etc/fstab').read_text().endswith(f'{node.file} none swap sw 0 0\n')
    assert (tmp_path / 'etc/sysctl.d/60-anysda-swap.conf').read_text() == 'vm.swappiness = 10\n'


def test_swap_fresh_node_falls_back_from_fallocate_to_dd(tmp_path, monkeypatch):
    node = SwapNode(tmp_path, monkeypatch)
    assert swap.ensure(tmp_path / 'none.json', lambda _text: None, root=tmp_path) is True
    assert node.calls == ['fallocate', 'mkswap', 'swapon', 'dd', 'mkswap', 'swapon']


def test_swap_skipped_in_container_and_with_active_swap(tmp_path, monkeypatch):
    node, lines = SwapNode(tmp_path, monkeypatch, virt='lxc'), []
    assert swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path) is False
    assert lines == ['swap пропущен: контейнер (lxc)']
    node.virt = 'none'
    (tmp_path / 'proc/swaps').write_text('Filename Type\n/swapfile file 1 0 -2\n')
    assert swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path) is False
    assert node.calls == []


def test_f2b_ignore_skips_garbage():
    assert host.f2b_ignore('127.0.0.1 1 22', 'entry.example', lambda _name: ['not-an-ip;rm']) == ['127.0.0.1/8', '::1']


def exit_venv(tmp_path, wheel_bytes, fail=''):
    """`vpn2 host venv` with a stub python: `-m venv` leaves bin/vpn2, a call with `fail` in it fails."""
    stub = tmp_path / 'python3'
    stub.write_text('#!/bin/sh\n'
                    f'echo "$*" >> {tmp_path}/calls\n'
                    f'case "$*" in *"{fail or "no such call"}"*) exit 1;; esac\n'
                    '[ "$1" = "-m" ] || exit 0\n'
                    'for dir; do :; done\n'
                    'mkdir -p "$dir/bin" && printf \'#!/bin/sh\\necho vpn2\\n\' > "$dir/bin/vpn2" '
                    '&& chmod +x "$dir/bin/vpn2"\n')
    stub.chmod(0o755)
    (tmp_path / 'pip').mkdir(exist_ok=True)
    (tmp_path / 'pip/pip-1-py3-none-any.whl').write_bytes(b'')
    wheel = tmp_path / 'vpn2-1-py3-none-any.whl'
    wheel.write_bytes(wheel_bytes)
    return host.main(['venv', str(wheel), '--root', str(tmp_path / 'root'),
                      '--pip-dir', str(tmp_path / 'pip'), '--python', str(stub)])


def exit_venv_runs(tmp_path):
    return subprocess.run([tmp_path / 'root/venv/bin/vpn2'], capture_output=True, text=True).stdout


def test_exit_venv_is_built_aside_and_switched_by_link(tmp_path, capsys):
    link = tmp_path / 'root/venv'
    assert exit_venv(tmp_path, b'one') == 0
    assert capsys.readouterr().out.endswith(host.CHANGED + '\n')
    first = link.resolve()
    assert link.is_symlink() and first.parent == (tmp_path / 'root/venvs').resolve()
    assert (link / '.vpn2-wheel').read_text() == hashlib.sha256(b'one').hexdigest() + '\n'
    calls = (tmp_path / 'calls').read_text()
    assert '--clear' not in calls and '--system-site-packages' in calls and '--no-index' in calls
    # the same wheel: nothing is called
    assert exit_venv(tmp_path, b'one') == 0
    assert host.CHANGED not in capsys.readouterr().out
    assert (tmp_path / 'calls').read_text() == calls and link.resolve() == first
    # another wheel: a new build, the old one is gone
    assert exit_venv(tmp_path, b'two') == 0
    assert link.resolve() != first and exit_venv_runs(tmp_path) == 'vpn2\n'
    assert [item.name for item in (tmp_path / 'root/venvs').iterdir()] == [link.resolve().name]


def test_exit_venv_aborted_build_leaves_the_current_one(tmp_path):
    link = tmp_path / 'root/venv'
    assert exit_venv(tmp_path, b'one') == 0
    first = link.resolve()
    assert exit_venv(tmp_path, b'two', fail=' install ') == 1
    assert link.resolve() == first and exit_venv_runs(tmp_path) == 'vpn2\n'
    assert (link / '.vpn2-wheel').read_text() == hashlib.sha256(b'one').hexdigest() + '\n'
    assert exit_venv(tmp_path, b'two') == 0
    assert [item.name for item in (tmp_path / 'root/venvs').iterdir()] == [link.resolve().name]


def test_exit_venv_directory_of_old_release_becomes_link(tmp_path):
    old = tmp_path / 'root/venv'
    old.mkdir(parents=True)
    (old / '.vpn2-wheel').write_text('old\n')
    assert exit_venv(tmp_path, b'one') == 0
    assert old.is_symlink() and exit_venv_runs(tmp_path) == 'vpn2\n'
    assert sorted(item.name for item in (tmp_path / 'root').iterdir()) == ['venv', 'venvs']
    assert len(list((tmp_path / 'root/venvs').iterdir())) == 1


def test_exit_venv_build_of_running_process_is_kept(tmp_path):
    assert exit_venv(tmp_path, b'one') == 0
    first = (tmp_path / 'root/venv').resolve()
    service = subprocess.Popen(['python3', '-c', 'import time; time.sleep(120)', f'{first}/bin/vpn2'])
    try:
        assert exit_venv(tmp_path, b'two') == 0
        assert first.is_dir()
    finally:
        service.kill()
        service.wait()
    assert exit_venv(tmp_path, b'three') == 0
    assert not first.exists()
