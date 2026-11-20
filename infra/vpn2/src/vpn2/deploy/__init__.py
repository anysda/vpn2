"""./deploy.sh на entry: оркестратор деплоя. Стадии - плеи infra/ansible/site.yml (тег = стадия),
служебные шаги вокруг них - infra/ansible/deploy.yml. Идёт системным python3 из дерева до venv.

Здесь - что делается на самом entry до стадий: перенос состояния старой раскладки, venv с
ansible и колесом vpn2, зависимости, ключи оркестратора.
"""
import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vpn2 import config, creds, wizard
from vpn2.config import STATE_DIR

INFRA = Path(os.environ.get('DEPLOY_ROOT') or Path(__file__).resolve().parents[3])
REPO = INFRA.parent
CONFIG = REPO / 'config.yaml'
ROOT = Path(os.environ.get('VPN2_ROOT') or '/opt/vpn2')
WHEELS_DIR = Path(os.environ.get('PYTHON_WHEELS_DIR') or '/usr/share/python-wheels')
# Колёса из WHEELS_DIR собраны под системный python3, а в PATH может быть чужой.
PYTHON = os.environ.get('VPN2_PYTHON') or '/usr/bin/python3'
R, G, Y, B, E = ('\033[31m', '\033[32m', '\033[33m', '\033[34m', '\033[0m') if sys.stderr.isatty() else ('',) * 5


def say(color, text, tag='deploy'):
    print(f'{color}[{tag}]{E} {text}', file=sys.stderr, flush=True)


def log(text, tag='deploy'):
    say(B, text, tag)


def ok(text, tag='deploy'):
    say(G, text, tag)


def warn(text, tag='deploy'):
    say(Y, text, tag)


def die(text, tag='deploy'):
    print(f'{R}[{tag}] FATAL:{E} {text}', file=sys.stderr, flush=True)
    raise SystemExit(1)


def step(text):
    print(f'{B}==>{E} {text}', flush=True)


def elapsed(t0):
    """Время с t0: «4m 7s», до минуты - «7s»."""
    seconds = int(time.time() - t0)
    return f'{seconds // 60}m {seconds % 60}s'.removeprefix('0m ')


def svc(script, *args, tag='deploy'):
    """Функции узла из infra/lib/anysda-svc.sh на самом entry; код возврата."""
    return subprocess.run(['bash', '-c', f'set -euo pipefail; HOST_TAG={tag}; source "$0"; {script}',
                           INFRA / 'lib/anysda-svc.sh', *args], stdin=subprocess.DEVNULL).returncode


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


@contextlib.contextmanager
def locked(path):
    with open(path, 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def migrate_state(infra=INFRA, state=STATE_DIR):
    """Состояние старой раскладки (infra/state/*, infra/.known_hosts) - в каталог состояния.

    Ничего не стирается: при разном содержимом главнее цель, источник уходит в checkout-old.
    Зовётся под замком деплоя до inventory.py --prepare; повтор после обрыва доводит перенос.
    """
    moves = [(infra / f'.{name}', state / name) for name in ('known_hosts', 'known_hosts.old')]
    (infra / 'state/deploy.lock').unlink(missing_ok=True)
    if (infra / 'state').is_dir():
        moves += [(item, state / item.name) for item in sorted((infra / 'state').iterdir()) if item.is_file()]
    for src, dst in moves:
        if not src.is_file():
            continue
        if not dst.exists():
            # Между файловыми системами rename не атомарен: копия рядом с целью и rename.
            shutil.copy2(src, f'{dst}.tmp')
            os.replace(f'{dst}.tmp', dst)
        elif src.read_bytes() != dst.read_bytes():
            keep = state / 'checkout-old' / dst.name
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, keep)
            print(f'⚠ состояние в checkout отличалось от {dst}, сохранено в {keep}', file=sys.stderr)
        src.unlink()
    if (state / 'known_hosts').is_file():
        (state / 'known_hosts').chmod(0o600)
    with contextlib.suppress(OSError):
        (infra / 'state').rmdir()


class VenvError(Exception):
    pass


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def venv_pins():
    """Отпечаток пинов venv: python и constraints.txt (как у sha256sum)."""
    version = subprocess.run([PYTHON, '-V'], capture_output=True, text=True)
    files = [f'{INFRA}/ansible/constraints.txt']
    text = version.stdout + version.stderr + ''.join(f'{sha256(path)}  {path}\n' for path in files)
    return hashlib.sha256(text.encode()).hexdigest()


def source_hash():
    package = INFRA / 'vpn2'
    files = [package / 'pyproject.toml', *sorted(p for p in (package / 'src').rglob('*.py') if '__pycache__' not in p.parts)]
    return hashlib.sha256(''.join(f'{sha256(path)} {path.relative_to(package)}\n' for path in files).encode()).hexdigest()


def wheel_path():
    found = sorted((ROOT / 'wheels').glob('vpn2-*-py3-none-any.whl'))
    return found[0] if found else None


def first(pattern):
    found = sorted(WHEELS_DIR.glob(pattern))
    if not found:
        raise VenvError(f'ansible: нет {pattern} в {WHEELS_DIR} (пакет python3-venv)')
    return found[0]


def call(out, *command):
    if subprocess.run([str(part) for part in command], stdin=subprocess.DEVNULL, stdout=out, stderr=out).returncode:
        raise VenvError(f'ansible: сбой {" ".join(map(str, command[:4]))}')


def retried(out, *command):
    for attempt in range(3):
        try:
            return call(out, *command)
        except VenvError:
            if attempt == 2:
                raise
            time.sleep(5)


def build_wheel(out):
    """Колесо vpn2 без сети и без pip: PEP 517 хук setuptools из системного whl, в копии исходников."""
    wheels = ROOT / 'wheels'
    wheels.mkdir(parents=True, exist_ok=True)
    with locked(wheels / '.lock'):
        src = source_hash()
        if wheel_path() and read(wheels / '.src-sha256') == src:
            return
        setuptools = first('setuptools-*.whl')
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(INFRA / 'vpn2', f'{tmp}/src', ignore=shutil.ignore_patterns('__pycache__'))
            os.mkdir(f'{tmp}/out')
            call(out, PYTHON, '-I', '-c', 'import sys, os; sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[2])\n'
                'from setuptools import build_meta\nbuild_meta.build_wheel(sys.argv[3])', setuptools, f'{tmp}/src', f'{tmp}/out')
            built = next(Path(tmp, 'out').glob('vpn2-*.whl'), None)
            if not built:
                raise VenvError('ansible: колесо vpn2 не собралось')
            for old in wheels.glob('vpn2-*.whl'):
                old.unlink()
            shutil.move(built, wheels / built.name)
            (wheels / '.src-sha256').write_text(f'{src}\n')


def prune_venvs():
    """Сносит сборки, кроме действующей и тех, чей путь в командной строке живого процесса,
    и коллекции ansible прежних выпусков."""
    shutil.rmtree(ROOT / 'collections', ignore_errors=True)
    keep = os.readlink(ROOT / 'venv') if (ROOT / 'venv').is_symlink() else ''
    running = b''
    for cmdline in Path('/proc').glob('[0-9]*/cmdline'):
        with contextlib.suppress(OSError):
            running += cmdline.read_bytes() + b'\n'
    for build in (ROOT / 'venvs').glob('*/'):
        if f'venvs/{build.name}' != keep and f'/venvs/{build.name}/'.encode() not in running:
            shutil.rmtree(build, ignore_errors=True)


def pip():
    return [PYTHON, f'{first("pip-*.whl")}/pip', '--disable-pip-version-check']


def fill_store(out, pins):
    """Колёса ansible под пины - в pip-wheels из сети; при тех же пинах они уже там."""
    store = ROOT / 'pip-wheels'
    if read(store / '.vpn2-pins') == pins:
        return
    shutil.rmtree(store, ignore_errors=True)
    store.mkdir(parents=True)
    retried(out, *pip(), 'download', '--only-binary', ':all:', '-d', store, '-c', INFRA / 'ansible/constraints.txt',
            'ansible-core', 'mitogen')
    (store / '.vpn2-pins').write_text(f'{pins}\n')


def install_venv(out):
    """venv - симлинк на сборку venvs/<пины>-<исходники>: подмена одним rename, действующая не трогается.

    Оборванная сборка остаётся без .vpn2-pins и пересобирается; .vpn2-wheel - отпечаток сборки,
    по нему роли перезапускают службы пакета. Сеть нужна только при смене пинов.
    """
    venv, pins = ROOT / 'venv', venv_pins()
    wheel = wheel_path()
    if not wheel:
        raise VenvError(f'ansible: колеса vpn2 нет в {ROOT}/wheels')
    stamp, had = f'{read(ROOT / "wheels/.src-sha256")} {pins}', read(venv / '.vpn2-pins')
    if had == pins and read(venv / '.vpn2-wheel') == stamp:
        prune_venvs()
        out.write('[deploy] ansible: пины те же, без загрузки\n')
        return
    new = ROOT / f'venvs/{pins[:12]}-{stamp[:12]}'
    if not (new / '.vpn2-pins').is_file():
        shutil.rmtree(new, ignore_errors=True)
        new.parent.mkdir(parents=True, exist_ok=True)
        fill_store(out, pins)
        call(out, PYTHON, '-m', 'venv', '--without-pip', new)
        call(out, *pip(), '--python', new / 'bin/python', 'install', '--no-index', '--find-links', ROOT / 'pip-wheels',
             '-c', INFRA / 'ansible/constraints.txt', 'ansible-core', 'mitogen', wheel)
    (new / '.vpn2-wheel').write_text(f'{stamp}\n')
    (new / '.vpn2-pins').write_text(f'{pins}\n')
    with contextlib.suppress(FileNotFoundError):
        os.unlink(f'{venv}.new')
    os.symlink(f'venvs/{new.name}', f'{venv}.new')
    os.replace(f'{venv}.new', venv)
    prune_venvs()
    if had == pins:
        out.write('[deploy] ansible: пины те же, колесо vpn2 обновлено\n')


def ensure_venv():
    """Колесо и venv под текущие пины: при тех же пинах - меньше секунды."""
    ROOT.mkdir(parents=True, exist_ok=True)
    path = ROOT / 'ansible-venv.log'
    with open(path, 'w') as out:
        try:
            # Новые пины - всё равно загрузка: она идёт, пока собирается колесо vpn2.
            pins = venv_pins()
            with ThreadPoolExecutor(1) as pool:
                early = pool.submit(fill_store, out, pins) if read(ROOT / 'venv/.vpn2-pins') != pins else None
                build_wheel(out)
                if early:
                    early.result()
            with locked(ROOT / '.lock'):
                install_venv(out)
            failed = None
        except (VenvError, OSError) as error:
            out.write(f'{error}\n')
            failed = error
    if failed:
        print(''.join(path.read_text(errors='replace').splitlines(True)[-20:]), end='', file=sys.stderr)
        die(f'ansible: venv не установился, лог {path}')


def apt_have(*packages):
    found = subprocess.run(['dpkg-query', '-W', '-f=${db:Status-Status}\\n', *packages], capture_output=True, text=True)
    return found.returncode == 0 and all(line == 'installed' for line in found.stdout.splitlines())


def install_prereqs(*extra):
    """Программы, нужные деплою на этой машине; extra - ещё команды, пакет зовётся как команда."""
    step('проверяю локальные зависимости')
    commands = ('ssh', 'curl', 'python3', 'openssl', *extra)
    missing = [name for name in commands if not shutil.which(name)]
    packages = ['openssh-client' if name == 'ssh' else name for name in missing]
    if not apt_have('python3-venv'):
        packages.append('python3-venv')
    policy = subprocess.run(['apt-cache', 'policy', 'python3-setuptools-whl'], capture_output=True, text=True).stdout
    candidate = next((line.split()[1] for line in policy.splitlines() if line.strip().startswith('Candidate:')), '')
    if not apt_have('python3-wheel') and subprocess.run(['dpkg', '--compare-versions', candidate, 'lt', '70.1']).returncode == 0:
        packages.append('python3-wheel')
    if packages:
        if not apt_have('ca-certificates'):
            packages.append('ca-certificates')
        if not Path('/etc/debian_version').is_file():
            die(f'не хватает: {" ".join(missing)}. На не-Debian/Ubuntu поставь руками.')
        print(f'  ставлю недостающее: {" ".join(missing)}', flush=True)
        os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
        code = svc('apt_prepare; command -v docker >/dev/null 2>&1 || docker_apt_source; apt_get update -qq\n'
                   'NEEDRESTART_SUSPEND=1 apt_get install -y -qq --no-install-recommends "$@" >/dev/null', *packages)
        if code:
            raise SystemExit(code)
    for name in commands:
        print(f'  {name:<10} {G}✓{E}', flush=True)
    # Docker свежего entry (им проверяются образы выпуска) и его compose ставятся, пока собирается venv.
    docker = docker_install()
    ensure_venv()
    if docker and docker.wait():
        die('Docker на entry не установился, причина выше')
    os.environ['VPN2_WHEEL'] = str(wheel_path())


def docker_install():
    """Docker свежего entry фоном: процесс установки или None - он уже есть."""
    if shutil.which('docker') or installed():
        return None
    return subprocess.Popen(['bash', '-c', 'set -euo pipefail; HOST_TAG=ru; source "$0"; docker_apt_source; apt_update_if_stale\n'
                             'DEBIAN_FRONTEND=noninteractive apt_get install -y -qq "${DOCKER_PKGS[@]}" "${DOCKER_SKIP[@]/#docker-compose-plugin-/docker-compose-plugin}" >/dev/null\n'
                             'systemctl enable --now docker', INFRA / 'lib/anysda-svc.sh'], stdin=subprocess.DEVNULL)


def dedupe(path):
    lines = list(dict.fromkeys(line for line in path.read_text().splitlines() if line.split()))
    path.with_name(f'{path.name}.u').write_text(''.join(f'{line}\n' for line in lines))
    os.replace(path.with_name(f'{path.name}.u'), path)
    path.chmod(0o600)


def pub():
    """Открытая часть ключа оркестратора: её кладут в authorized_keys экзитов (./deploy.sh -key)."""
    return Path.home() / '.ssh/id_ed25519.pub'


def installed():
    """На этой машине уже стоит entry: есть база панели или /etc/anysda."""
    return Path('/var/lib/anysda-vpn2/db.sqlite').exists() or Path('/etc/anysda').is_dir()


def orchestrator_key(create=False):
    """Приватный ключ оркестратора (вид хранилища на tmpfs): им entry входит на себя и на экзиты.
    create - ключа нет, создать; есть - не трогать. Открытая часть - pub() и authorized_keys самой entry."""
    if not create:
        return creds.key_file('orchestrator_key')
    public = pub()
    public.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    creds.adopt()
    key = creds.key_file('orchestrator_key')
    if not key.is_file():
        log('ключа оркестратора нет — создаю ed25519')
        creds.VIEWS.mkdir(mode=0o755, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=creds.VIEWS) as work:
            subprocess.run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-C', 'anysda-orchestrator', '-f', f'{work}/key', '-q'],
                           check=True)
            creds.write('orchestrator_key', Path(work, 'key').read_text())
        key = creds.key_file('orchestrator_key')
    derive_pub(key, public)
    authorized = public.parent / 'authorized_keys'
    if key_id(public.read_text()) not in map(key_id, read_raw(authorized).splitlines()):
        with open(authorized, 'a') as keys:
            keys.write(public.read_text())
        authorized.chmod(0o600)
    return key


def derive_pub(key, public):
    """Открытая часть - из ключа хранилища: restore мог сменить ключ. Строку прежнего ключа authorized_keys
    entry теряет, иначе она сошла бы за ключ человека (human_keys)."""
    text = subprocess.run(['ssh-keygen', '-y', '-f', key], check=True, capture_output=True, text=True).stdout
    old = key_id(read_raw(public))
    if old == key_id(text):
        return
    authorized = public.parent / 'authorized_keys'
    if old and authorized.is_file():
        authorized.write_text(''.join(line for line in authorized.read_text().splitlines(keepends=True)
                                      if key_id(line) != old))
    public.write_text(text)


def ssh_options(state=STATE_DIR):
    """Параметры входа оркестратора на узлы: у ansible (inventory.py), у pre-flight и у менеджера установки."""
    return ['-o', f'UserKnownHostsFile={state}/known_hosts', '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'PreferredAuthentications=publickey', '-o', 'ConnectTimeout=10']


def take_secrets():
    """Секреты из мест 1.2.0 и из области secrets config.yaml - в хранилище, до стадий; файл стирается только
    после успешного деплоя (finish_secrets). Вернуть пароль админа из файла: в хранилище он не идёт.

    Место 1.2.0 в config.yaml применяется, только пока секрета в хранилище нет: действовавший в 1.2.0 токен бота
    и секрет SSO из панели уже взял adopt().
    """
    creds.adopt()
    try:
        cfg = config.load(CONFIG)
    except (OSError, config.ConfigError):
        return ''
    found, area = config.secrets(cfg), cfg.raw.get('secrets', {})
    # Пароль, введённый в мастере установки: файл передачи 0600, стирается после стадии панели.
    password = found.pop('admin_password', '') or read(wizard.HANDOVER / 'admin_password')
    for name, value in found.items():
        if area.get(name) or not creds.read(name):
            creds.write(name, value)
    return password


def finish_secrets(panel=True):
    """Деплой прошёл: область secrets пуста, места 1.2.0 убраны из config.yaml. Пароль админа в хранилище не идёт:
    без стадии панели (panel=False) он остаётся в файле до прогона, который его применит."""
    keep = () if panel else ('admin_password',)
    if panel:
        (wizard.HANDOVER / 'admin_password').unlink(missing_ok=True)
    return config.wipe_secrets(CONFIG, keep) if CONFIG.is_file() else False


def admin_key_check(env):
    """Вход по паролю закрывается всегда, поэтому без ключа человека деплой не начинается. На установленной entry
    (обновление с 1.2.0) хватает ключа, что уже лежит в /root/.ssh/authorized_keys."""
    key = env.get('ADMIN_SSH_PUBKEY', '')
    if key and not key_id(key):
        die('admin.ssh_pubkey в config.yaml - не открытый ключ ssh (ожидается строка вида «ssh-ed25519 AAAA... имя»)')
    if key or (installed() and human_keys(read_raw(pub().parent / 'authorized_keys'), *own_keys())):
        return
    die('admin.ssh_pubkey в config.yaml не задан. Вход на серверы по паролю деплой закрывает, '
        'поэтому без ключа на entry будет не зайти. Впиши свой открытый ключ ssh '
        '(cat ~/.ssh/id_ed25519.pub на своём компьютере) в admin.ssh_pubkey и повтори ./deploy.sh')


def own_keys():
    """Тексты .pub ключей самого узла: оркестратора, старого RSA и mgmt."""
    ssh = Path.home() / '.ssh'
    return [read_raw(path) for path in (pub(), ssh / 'id_rsa.pub', '/root/.ssh/id_ed25519_mgmt.pub')]


def prep_keys():
    """Перед 00-bootstrap: ключи для authorized_keys узлов и ключ entry для ssh на экзиты через mgmt (отдельный,
    приватная часть - в хранилище, как у ключа оркестратора)."""
    out = INFRA / 'secrets/rendered'
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    orchestrator_key(create=True)
    ssh = pub().parent
    authorized = ssh / 'authorized_keys'
    (out / 'orchestrator_keys').write_text(''.join(read_raw(path) for path in (pub(), ssh / 'id_rsa.pub', authorized)))
    for path in (authorized, out / 'orchestrator_keys'):
        dedupe(path)
    mgmt = Path('/root/.ssh/id_ed25519_mgmt.pub')
    if not creds.read('mgmt_key'):
        with tempfile.TemporaryDirectory(dir=creds.VIEWS) as work:
            subprocess.run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-q', '-C', 'anysda-entry-mgmt', '-f', f'{work}/key'],
                           check=True)
            creds.write('mgmt_key', Path(work, 'key').read_text())
    derive_pub(creds.key_file('mgmt_key'), mgmt)
    shutil.copyfile(mgmt, out / 'entry_mgmt_key')
    (out / 'entry_mgmt_key').chmod(0o600)
    # Ключ человека на entry - не свой ключ узла: заданный у хостера или админом.
    (out / 'admin_keys').write_text(human_keys(read_raw(authorized), *own_keys()))


def human_keys(authorized, *own):
    """Строки authorized_keys, кроме ключей самого узла (own - тексты его .pub)."""
    skip = {key_id(text) for text in own} | {None}
    return ''.join(f'{line}\n' for line in authorized.splitlines() if key_id(line) not in skip)


def key_id(line):
    """Тип и тело ключа из строки authorized_keys или .pub: опции и комментарий не в счёт."""
    found = re.search(r'(?:^|\s)((?:ssh|ecdsa|sk)-\S+ [A-Za-z0-9+/=]+)', line)
    return found[1] if found else None


def read_raw(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ''
