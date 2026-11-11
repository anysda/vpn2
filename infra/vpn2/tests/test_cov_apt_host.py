"""apt на узле: кто держит блокировку, свежесть списков, источник Docker, установка недостающего.

Системные команды подменены программами на PATH с состоянием в файлах:
пакет зовёт их так же, как на узле.
"""
import os
import subprocess
import sys
import time

import pytest

from vpn2 import apt

TOOL = r'''#!{python}
import os, sys
box = {box!r}
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]


def read(file):
    try:
        return open(os.path.join(box, file)).read()
    except OSError:
        return ''


open(os.path.join(box, 'calls'), 'a').write(' '.join([name, *args]).replace('\n', '') + '\n')
if name in ('fuser', 'lslocks'):
    sys.stdout.write(read(name + '.out'))
elif name == 'dpkg':
    print('amd64')
elif name == 'dpkg-query':
    have = read('installed').split()
    code = 0
    for package in args[2:]:
        if package in have:
            print('installed')
        else:
            code = 1
    sys.exit(code)
elif name == 'apt-get':
    args = args[2:]
    code = int(read('apt.rc') or 0)
    if args[0] == 'install' and code == 0:
        new = [a for a in args[1:] if not a.startswith('-') and not a.endswith('-')]
        open(os.path.join(box, 'installed'), 'a').write('\n'.join(new) + '\n')
    sys.stderr.write(read('apt.err'))
    sys.exit(code)
elif name == 'curl':
    left = int(read('curl.fails') or 0)
    if left > 0:
        open(os.path.join(box, 'curl.fails'), 'w').write(str(left - 1))
    if read('curl.rc') or left > 0:
        sys.exit(22)
    sys.stdout.write(read('curl.out') if os.path.exists(os.path.join(box, 'curl.out'))
                     else 'ARMORED ' + args[-1])
elif name == 'gpg':
    body = sys.stdin.read()
    if read('gpg.rc'):
        open(args[args.index('-o') + 1], 'w').write('KE')
        sys.exit(2)
    open(args[args.index('-o') + 1], 'w').write('KEY ' + body if body else '')
'''
NAMES = ('fuser', 'lslocks', 'dpkg', 'dpkg-query', 'apt-get', 'curl', 'gpg')
OS_RELEASE = 'PRETTY_NAME="Ubuntu 26.04"\nID=ubuntu\nVERSION_CODENAME=resolute\n'
CURL = 'curl -fsSL --connect-timeout 20 --max-time 120 https://download.docker.com/linux/ubuntu/gpg'
LINE = ('deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] '
        'https://download.docker.com/linux/ubuntu resolute stable\n')


class Host:
    def __init__(self, path):
        self.box = path / 'box'
        self.bin = path / 'bin'
        self.root = path / 'root'
        for directory in (self.box, self.bin, self.root / 'etc'):
            directory.mkdir(parents=True)
        (self.root / 'etc/os-release').write_text(OS_RELEASE)
        tool = self.box / 'tool'
        tool.write_text(TOOL.format(python=sys.executable, box=str(self.box)))
        tool.chmod(0o755)
        for name in NAMES:
            (self.bin / name).symlink_to(tool)

    def put(self, name, text):
        (self.box / name).write_text(text)

    def calls(self, program=None):
        path = self.box / 'calls'
        lines = path.read_text().splitlines() if path.exists() else []
        path.write_text('')
        lines = [line for line in lines if not line.endswith('update -qq')]
        return [line for line in lines if program is None or line.split()[0] == program]


@pytest.fixture
def host(tmp_path, monkeypatch):
    box = Host(tmp_path)
    monkeypatch.setenv('PATH', str(box.bin))
    monkeypatch.setenv('HOST_TAG', 'ru')
    monkeypatch.setattr(apt, 'GAVE_UP', tmp_path / 'gave-up')
    return box


def test_free_apt_has_no_holders(host):
    assert apt.lock_holders() == []
    assert host.calls() == ['fuser ' + ' '.join(apt.LOCKS)]


def test_holders_are_unique_sorted_pids(host):
    host.put('fuser.out', ' 4021  77 4021\n 12 garbage\n')
    assert apt.lock_holders() == [12, 77, 4021]


def test_without_fuser_holders_come_from_lslocks(host):
    (host.bin / 'fuser').unlink()
    host.put('lslocks.out', ' 812 /var/lib/dpkg/lock-frontend\n'
                            ' 812 /var/lib/dpkg/lock\n'
                            '  40 /run/other.lock\n'
                            'oops /var/lib/dpkg/lock\n'
                            ' 700 /var/lib/apt/lists/lock extra\n'
                            '\n   9 /var/cache/apt/archives/lock\n')
    assert apt.lock_holders() == [9, 812]
    host.put('lslocks.out', '')
    assert apt.lock_holders() == []


def lists(tmp_path, *, age=0, index=True):
    directory = tmp_path / 'lists'
    directory.mkdir()
    if index:
        (directory / 'archive.ubuntu.com_dists_resolute_main_binary-amd64_Packages.lz4').write_text('x')
    stamp = time.time() - age
    os.utime(directory, (stamp, stamp))
    return directory


def source(tmp_path, *, newer_than=None):
    path = tmp_path / 'sources.list'
    path.write_text('deb http://archive.ubuntu.com/ubuntu resolute main\n')
    stamp = time.time() - 7200 if newer_than is None else newer_than.stat().st_mtime + 5
    os.utime(path, (stamp, stamp))
    return path


def test_fresh_lists_need_no_update(host, tmp_path):
    directory = lists(tmp_path, age=60)
    for _ in range(2):
        assert apt.update_if_stale(lists=directory, sources=[source(tmp_path)]) == 0
    assert (host.box / 'calls').exists() is False


@pytest.mark.parametrize('case', ['absent', 'old', 'no-index', 'source-newer', 'source-dir-newer'])
def test_stale_lists_are_updated(host, tmp_path, case):
    directory = tmp_path / 'lists'
    sources = [tmp_path / 'no-such-source']
    if case == 'old':
        directory = lists(tmp_path, age=3601)
    elif case == 'no-index':
        directory = lists(tmp_path, index=False)
    elif case == 'source-newer':
        directory = lists(tmp_path, age=60)
        sources = [source(tmp_path, newer_than=directory)]
    elif case == 'source-dir-newer':
        directory = lists(tmp_path, age=60)
        parts = tmp_path / 'sources.list.d'
        parts.mkdir()
        old = time.time() - 7200
        os.utime(parts, (old, old))
        (parts / 'docker.list').write_text('deb x\n')
        os.utime(parts, (old, old))
        sources = [parts]
    assert apt.lists_are_fresh(directory, sources) is False
    assert apt.update_if_stale(lists=directory, sources=sources) == 0
    assert (host.box / 'calls').read_text().splitlines()[-1].endswith('update -qq')


def test_failed_update_returns_the_apt_code(host, tmp_path, capfd):
    host.put('apt.rc', '100')
    host.put('apt.err', 'E: Could not open lock file\n')
    assert apt.update_if_stale(lists=tmp_path / 'lists', sources=[]) == 100
    assert 'E: Could not open lock file' in capfd.readouterr().err


def test_present_packages_are_not_installed_again(host):
    host.put('installed', 'jq\ncurl\n')
    assert apt.install(['jq', 'curl']) is False
    assert host.calls('apt-get') == []


def test_empty_list_installs_nothing(host):
    assert apt.install([]) is False
    assert host.calls('apt-get') == []


def test_missing_package_is_installed_once(host):
    host.put('installed', 'jq\n')
    assert apt.install(['jq', 'wireguard-tools']) is True
    assert host.calls('apt-get') == [
        f'apt-get -o DPkg::Lock::Timeout={apt.LOCK_TIMEOUT} install -y -qq jq wireguard-tools']
    assert apt.install(['jq', 'wireguard-tools']) is False
    assert host.calls('apt-get') == []


def test_failed_install_names_the_code(host):
    host.put('apt.rc', '100')
    with pytest.raises(RuntimeError, match='apt-get install: код 100'):
        apt.install(['jq'])


def test_busy_apt_gets_the_short_lock_timeout_after_giving_up(host, tmp_path, capsys):
    host.put('fuser.out', '4021\n')
    (tmp_path / 'gave-up').write_text('')
    assert apt.install(['jq']) is True
    assert host.calls('apt-get') == [
        f'apt-get -o DPkg::Lock::Timeout={apt.GAVE_UP_LOCK_TIMEOUT} install -y -qq jq']
    assert '[ru]   apt всё ещё занят, ожидание уже исчерпано, продолжаю' in capsys.readouterr().out


def test_docker_source_is_written_once(host):
    host.put('installed', 'ca-certificates\ncurl\ngnupg\n')
    apt.docker_source(root=host.root)
    target = host.root / 'etc/apt/sources.list.d/docker.list'
    key = host.root / 'etc/apt/keyrings/docker.gpg'
    assert target.read_text() == LINE
    assert key.read_text() == 'KEY ARMORED https://download.docker.com/linux/ubuntu/gpg'
    assert key.stat().st_mode & 0o444 == 0o444
    assert host.calls('curl') == [CURL]
    before = (target.stat().st_mtime_ns, key.stat().st_mtime_ns)
    apt.docker_source(root=host.root)
    assert (target.stat().st_mtime_ns, key.stat().st_mtime_ns) == before
    assert host.calls('curl') == [] and host.calls('apt-get') == []


def test_docker_source_repairs_a_foreign_line_and_an_empty_key(host):
    host.put('installed', 'ca-certificates\ncurl\ngnupg\n')
    target = host.root / 'etc/apt/sources.list.d/docker.list'
    target.parent.mkdir(parents=True)
    target.write_text('deb https://example.invalid stale\n')
    key = host.root / 'etc/apt/keyrings/docker.gpg'
    key.parent.mkdir(parents=True)
    key.write_text('')
    apt.docker_source(root=host.root)
    assert target.read_text() == LINE and key.read_text().startswith('KEY ')


def test_docker_source_installs_its_tools_first(host):
    apt.docker_source(root=host.root)
    assert host.calls('apt-get') == [
        f'apt-get -o DPkg::Lock::Timeout={apt.LOCK_TIMEOUT} install -y -qq ca-certificates curl gnupg']
    assert (host.root / 'etc/apt/sources.list.d/docker.list').read_text() == LINE


def test_docker_source_without_tools_is_refused(host):
    host.put('apt.rc', '100')
    with pytest.raises(RuntimeError, match='не поставились ca-certificates curl gnupg'):
        apt.docker_source(root=host.root)
    assert not (host.root / 'etc/apt/sources.list.d/docker.list').exists()


@pytest.mark.parametrize(('text', 'reason'), [
    ('', 'неподдерживаемый ID в /etc/os-release: $'),
    ('ID=fedora\nVERSION_CODENAME=forty\n', 'неподдерживаемый ID в /etc/os-release: fedora'),
    ('ID=debian\n', 'нет корректного VERSION_CODENAME'),
    ('ID=debian\nVERSION_CODENAME="bad name"\n', 'нет корректного VERSION_CODENAME'),
    ('garbage without equals\n', 'неподдерживаемый ID'),
])
def test_foreign_system_is_refused_before_any_change(host, text, reason):
    (host.root / 'etc/os-release').write_text(text)
    with pytest.raises(ValueError, match=reason):
        apt.docker_source(root=host.root)
    assert host.calls('apt-get') == [] and not (host.root / 'etc/apt').exists()


def test_failed_key_download_leaves_no_source(host, capfd):
    host.put('installed', 'ca-certificates\ncurl\ngnupg\n')
    host.put('curl.rc', '1')
    pauses = []
    with pytest.raises(subprocess.CalledProcessError, match='curl'):
        apt.docker_source(root=host.root, sleep=pauses.append)
    assert not (host.root / 'etc/apt/sources.list.d/docker.list').exists()
    assert not (host.root / 'etc/apt/keyrings/docker.gpg').exists()
    assert host.calls('curl') == [CURL] * apt.FETCH_TRIES and pauses == [5, 10]
    err = capfd.readouterr().err
    assert ('[ru]   ключ Docker не скачался с https://download.docker.com/linux/ubuntu/gpg '
            'за 3 попытки: curl, код 22') in err
    host.put('curl.rc', '')
    apt.docker_source(root=host.root, sleep=pauses.append)
    assert (host.root / 'etc/apt/sources.list.d/docker.list').read_text() == LINE
    assert (host.root / 'etc/apt/keyrings/docker.gpg').read_text().startswith('KEY ')


@pytest.mark.parametrize('fails', [1, 2])
def test_key_download_survives_a_transient_failure(host, fails, capsys):
    host.put('installed', 'ca-certificates\ncurl\ngnupg\n')
    host.put('curl.fails', str(fails))
    pauses = []
    apt.docker_source(root=host.root, sleep=pauses.append)
    assert host.calls('curl') == [CURL] * (fails + 1) and pauses == [5, 10][:fails]
    assert (host.root / 'etc/apt/keyrings/docker.gpg').read_text().startswith('KEY ARMORED')
    assert (host.root / 'etc/apt/sources.list.d/docker.list').read_text() == LINE
    assert '[ru]   ключ Docker: curl, код 22, повтор через 5 с' in capsys.readouterr().out


@pytest.mark.parametrize('case', ['gpg-cut', 'empty-body'])
def test_cut_key_leaves_no_partial_file(host, case):
    host.put('installed', 'ca-certificates\ncurl\ngnupg\n')
    host.put('gpg.rc' if case == 'gpg-cut' else 'curl.out', '1' if case == 'gpg-cut' else '')
    with pytest.raises((subprocess.CalledProcessError, RuntimeError)):
        apt.docker_source(root=host.root)
    keyrings = host.root / 'etc/apt/keyrings'
    assert list(keyrings.iterdir()) == []
    assert not (host.root / 'etc/apt/sources.list.d/docker.list').exists()
    (host.box / ('gpg.rc' if case == 'gpg-cut' else 'curl.out')).unlink()
    apt.docker_source(root=host.root)
    assert [path.name for path in keyrings.iterdir()] == ['docker.gpg']


def test_entry_with_docker_keeps_its_sources_alone(host):
    (host.bin / 'docker').symlink_to(host.box / 'tool')
    host.put('installed', 'jq\n')
    assert apt.install(['jq'], docker=True) is False
    assert host.calls('curl') == [] and not (host.root / 'etc/apt').exists()


def test_entry_without_docker_gets_source_and_packages(host, monkeypatch):
    real = apt.docker_source
    monkeypatch.setattr(apt, 'docker_source', lambda **kw: real(root=host.root, **kw))
    host.put('installed', 'ca-certificates\ncurl\ngnupg\njq\n')
    assert apt.install(['jq'], docker=True) is True
    calls = host.calls()
    assert [line for line in calls if line.startswith('curl')] == [CURL]
    assert [line for line in calls if line.startswith('apt-get')] == [
        f'apt-get -o DPkg::Lock::Timeout={apt.LOCK_TIMEOUT} install -y -qq jq '
        + ' '.join(apt.DOCKER_PKGS + apt.DOCKER_SKIP)]
    assert (host.root / 'etc/apt/sources.list.d/docker.list').read_text() == LINE
