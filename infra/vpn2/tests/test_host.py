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
