"""apt и dpkg на узле: ожидание блокировок, повтор загрузки, свежесть списков."""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

LOCKS = ('/var/lib/dpkg/lock-frontend', '/var/lib/dpkg/lock',
         '/var/lib/apt/lists/lock', '/var/cache/apt/archives/lock')
LOCK_TIMEOUT = 1200
# Ожидание уже исчерпано в этом деплое: следующий вызов не ждёт второй круг,
# а apt-get получает короткий Lock::Timeout и либо идёт, либо падает сразу.
GAVE_UP = Path('/run/anysda-apt-gave-up')
GAVE_UP_TTL = 3600
GAVE_UP_LOCK_TIMEOUT = 60
FETCH_TRIES = 3
DOCKER_PKGS = ('docker-ce', 'docker-ce-cli', 'containerd.io')
# buildx, compose и rootless-extras на узле не зовёт никто, а тянут их Recommends.
DOCKER_SKIP = ('docker-ce-rootless-extras-', 'docker-buildx-plugin-', 'docker-compose-plugin-')
ENV = {'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'a'}

_FETCH_RE = re.compile(r'^[WE]: Failed to fetch +(\S+) +(.*)$')


def say(text):
    print(f"[{os.environ.get('HOST_TAG', 'anysda')}]   {text}", flush=True)


def _run(argv, **kwargs):
    kwargs.setdefault('text', True)
    return subprocess.run(argv, **kwargs)


def lock_holders(locks=LOCKS):
    """PID держателей блокировок apt/dpkg; пусто - apt свободен."""
    if shutil.which('fuser'):
        out = _run(['fuser', *locks], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
        return sorted({int(word) for word in out.split() if word.isdigit()})
    # В минимальном Debian нет psmisc; lslocks видит те же блокировки ядра.
    out = _run(['lslocks', '--noheadings', '--notruncate', '--output', 'PID,PATH'],
               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    found = set()
    for line in out.splitlines():
        words = line.split()
        if len(words) == 2 and words[1] in locks and words[0].isdigit():
            found.add(int(words[0]))
    return sorted(found)


def _cmdline(pid):
    try:
        return Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
    except OSError:
        return ''


def gave_up(marker=None, *, now=None):
    marker = GAVE_UP if marker is None else marker
    try:
        age = (time.time() if now is None else now) - Path(marker).stat().st_mtime
    except OSError:
        return False
    return age < GAVE_UP_TTL


def _mark(marker, on):
    try:
        if on:
            Path(marker).write_text('')
        else:
            Path(marker).unlink(missing_ok=True)
    except OSError:
        pass


def wait_idle(*, timeout=LOCK_TIMEOUT, marker=None, holders=lock_holders,
              sleep=time.sleep, clock=time.monotonic, say=say):
    """Ждёт, пока apt освободится; True - свободен, False - ожидание исчерпано.

    Исчерпанное ожидание запоминается в marker: повторный вызов в том же деплое
    не ждёт заново. Свободный apt метку снимает.
    """
    marker = GAVE_UP if marker is None else marker
    start = clock()
    said = None
    while True:
        pids = holders()
        waited = int(clock() - start)
        if not pids:
            _mark(marker, False)
            if waited >= 5:
                say(f'apt освободился через {waited} с')
            return True
        if gave_up(marker):
            say('apt всё ещё занят, ожидание уже исчерпано, продолжаю')
            return False
        if waited >= timeout:
            say(f'apt так и не освободился за {waited} с, продолжаю')
            _mark(marker, True)
            return False
        if said is None or waited - said >= 20:
            said = waited
            for pid in pids:
                say(f'apt занят, жду {pid} {_cmdline(pid)}{waited} с')
        sleep(5)


def fetch_failure(stderr):
    """Первый сбой загрузки, который стоит повторить: 5xx, 408, 429, обрыв сети."""
    for line in stderr.splitlines():
        match = _FETCH_RE.match(line)
        if not match:
            continue
        url, rest = match.groups()
        code = rest.split()[0] if rest.split() else ''
        if re.fullmatch(r'4\d\d', code) and code not in ('408', '429'):
            continue
        rest = re.sub(r' +\[IP: .*', '', rest)
        return f"{url}: {re.sub(r'  +', ' ', rest)}"
    return None


def apt_get(args, *, quiet=False, marker=None, wait=wait_idle, sleep=time.sleep, say=say, run=None):
    """apt-get после ожидания блокировок, с повтором сбоя загрузки; возвращает код."""
    run = run or _run
    marker = GAVE_UP if marker is None else marker
    attempt = 1
    while True:
        idle = wait(marker=marker)
        lock_timeout = LOCK_TIMEOUT if idle else GAVE_UP_LOCK_TIMEOUT
        result = run(['apt-get', '-o', f'DPkg::Lock::Timeout={lock_timeout}', *args],
                     env=dict(os.environ, **ENV), stderr=subprocess.PIPE,
                     stdout=subprocess.DEVNULL if quiet else None)
        failed = fetch_failure(result.stderr or '')
        if failed is None:
            break
        if attempt >= FETCH_TRIES:
            sys.stderr.write(result.stderr or '')
            print(f"[{os.environ.get('HOST_TAG', 'anysda')}]   зеркало apt не отдало {failed} "
                  f'за {attempt} попытки', file=sys.stderr)
            return result.returncode
        say(f'зеркало apt: {failed}, повтор через {5 * attempt} с')
        sleep(5 * attempt)
        attempt += 1
    sys.stderr.write(result.stderr or '')
    return result.returncode


def have(packages, *, run=None):
    """Все пакеты установлены: apt-get install поверх них всё равно читает списки."""
    run = run or _run
    result = run(['dpkg-query', '-W', '-f=${db:Status-Status}\n', *packages],
                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        return False
    return all(line == 'installed' for line in result.stdout.splitlines())


def lists_are_fresh(lists, sources, *, now=None):
    """Каталог списков обновлён за час, источники не новее его, индекс пакетов есть."""
    lists = Path(lists)
    try:
        stamp = lists.stat().st_mtime
    except OSError:
        return False
    if (time.time() if now is None else now) - stamp >= 3600:
        return False
    for source in sources:
        source = Path(source)
        paths = [source, *source.rglob('*')] if source.is_dir() else [source]
        for path in paths:
            try:
                if path.lstat().st_mtime > stamp:
                    return False
            except OSError:
                continue
    return any(path.is_file() for path in lists.glob('*_Packages*'))


def update_if_stale(*, lists='/var/lib/apt/lists',
                    sources=('/etc/apt/sources.list', '/etc/apt/sources.list.d'), get=apt_get):
    if lists_are_fresh(lists, sources):
        return 0
    return get(['update', '-qq'])


def docker_source_line(os_release, arch):
    """Строка источника Docker по тексту /etc/os-release; ValueError для чужой ОС."""
    fields = {}
    for line in os_release.splitlines():
        key, sep, value = line.partition('=')
        if sep:
            fields[key.strip()] = value.strip().strip('"\'')
    distro, codename = fields.get('ID', ''), fields.get('VERSION_CODENAME', '')
    if distro not in ('debian', 'ubuntu'):
        raise ValueError(f'Docker: неподдерживаемый ID в /etc/os-release: {distro}')
    if not re.fullmatch(r'[a-z][a-z0-9-]*', codename):
        raise ValueError('Docker: нет корректного VERSION_CODENAME в /etc/os-release')
    return (distro, f'deb [arch={arch} signed-by=/etc/apt/keyrings/docker.gpg] '
                    f'https://download.docker.com/linux/{distro} {codename} stable')


def _docker_key(url, key, *, run, sleep, say):
    """Ключ Docker: загрузка с повтором, запись целиком или никак."""
    for attempt in range(1, FETCH_TRIES + 1):
        try:
            armored = run(['curl', '-fsSL', '--connect-timeout', '20', '--max-time', '120', url],
                          stdout=subprocess.PIPE, check=True, text=False).stdout
            break
        except subprocess.CalledProcessError as error:
            if attempt >= FETCH_TRIES:
                print(f"[{os.environ.get('HOST_TAG', 'anysda')}]   ключ Docker не скачался с {url} "
                      f'за {attempt} попытки: curl, код {error.returncode}', file=sys.stderr)
                raise
            say(f'ключ Docker: curl, код {error.returncode}, повтор через {5 * attempt} с')
            sleep(5 * attempt)
    part = key.with_name(key.name + '.part')
    try:
        subprocess.run(['gpg', '--dearmor', '--yes', '-o', str(part)], input=armored, check=True)
        if part.stat().st_size == 0:
            raise RuntimeError(f'Docker: по адресу {url} пришёл пустой ключ')
        part.chmod(part.stat().st_mode | 0o444)
        os.replace(part, key)
    finally:
        part.unlink(missing_ok=True)


def docker_source(*, root='/', get=apt_get, run=None, sleep=time.sleep, say=say):
    """Официальный источник Docker; файл переписывается только при отличии."""
    run = run or _run
    root = Path(root)
    arch = run(['dpkg', '--print-architecture'], stdout=subprocess.PIPE, check=True).stdout.strip()
    distro, line = docker_source_line((root / 'etc/os-release').read_text(), arch)
    if not have(['ca-certificates', 'curl', 'gnupg'], run=run):
        get(['update', '-qq'])
        if get(['install', '-y', '-qq', 'ca-certificates', 'curl', 'gnupg'], quiet=True) != 0:
            raise RuntimeError('Docker: не поставились ca-certificates curl gnupg')
    keyrings = root / 'etc/apt/keyrings'
    keyrings.mkdir(mode=0o755, parents=True, exist_ok=True)
    key = keyrings / 'docker.gpg'
    if not key.is_file() or key.stat().st_size == 0:
        _docker_key(f'https://download.docker.com/linux/{distro}/gpg', key,
                    run=run, sleep=sleep, say=say)
    target = root / 'etc/apt/sources.list.d/docker.list'
    try:
        current = target.read_text()
    except OSError:
        current = None
    if current != line + '\n':
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(line + '\n')


def install(packages, *, docker=False, get=apt_get, run=None):
    """Ставит недостающее одним вызовом apt; True - что-то ставилось.

    docker=True добавляет Docker из его источника, если команды docker нет.
    """
    packages = list(packages)
    extra = []
    if docker and not shutil.which('docker'):
        docker_source(get=get, run=run)
        packages += DOCKER_PKGS
        extra = list(DOCKER_SKIP)
    if have(packages, run=run):
        return False
    update_if_stale(get=get)
    code = get(['install', '-y', '-qq', *packages, *extra], quiet=True)
    if code != 0:
        raise RuntimeError(f'apt-get install: код {code}')
    return True
