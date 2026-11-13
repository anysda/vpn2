"""./deploy.sh на entry: оркестратор деплоя. Стадии - плеи infra/ansible/site.yml (тег = стадия),
служебные шаги вокруг них - infra/ansible/deploy.yml. Идёт системным python3 из дерева до venv.

Здесь - что делается на самом entry до стадий: перенос состояния старой раскладки, venv с
ansible и колесом vpn2, зависимости, ключи оркестратора.
"""
import base64
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
from pathlib import Path

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
    """Отпечаток пинов venv: python, constraints.txt и requirements.yml (как у sha256sum)."""
    version = subprocess.run([PYTHON, '-V'], capture_output=True, text=True)
    files = [f'{INFRA}/ansible/constraints.txt', f'{INFRA}/ansible/requirements.yml']
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
            for old in wheels.glob('vpn2-*.whl'):
                old.unlink()
            built = next(Path(tmp, 'out').glob('vpn2-*.whl'))
            shutil.move(built, wheels / built.name)
            (wheels / '.src-sha256').write_text(f'{src}\n')


def prune_venvs():
    """Сносит сборки, кроме действующей и тех, чей путь в командной строке живого процесса."""
    keep = os.readlink(ROOT / 'venv') if (ROOT / 'venv').is_symlink() else ''
    running = b''
    for cmdline in Path('/proc').glob('[0-9]*/cmdline'):
        with contextlib.suppress(OSError):
            running += cmdline.read_bytes() + b'\n'
    for build in (ROOT / 'venvs').glob('*/'):
        if f'venvs/{build.name}' != keep and f'/venvs/{build.name}/'.encode() not in running:
            shutil.rmtree(build, ignore_errors=True)


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
    pip = [PYTHON, f'{first("pip-*.whl")}/pip', '--disable-pip-version-check']
    new, store, constraints = ROOT / f'venvs/{pins[:12]}-{stamp[:12]}', ROOT / 'pip-wheels', INFRA / 'ansible/constraints.txt'
    if not (new / '.vpn2-pins').is_file():
        shutil.rmtree(new, ignore_errors=True)
        new.parent.mkdir(parents=True, exist_ok=True)
        if read(store / '.vpn2-pins') != pins:
            shutil.rmtree(store, ignore_errors=True)
            store.mkdir(parents=True)
            retried(out, *pip, 'download', '--only-binary', ':all:', '-d', store, '-c', constraints, 'ansible-core')
            (store / '.vpn2-pins').write_text(f'{pins}\n')
        call(out, PYTHON, '-m', 'venv', '--without-pip', new)
        call(out, *pip, '--python', new / 'bin/python', 'install', '--no-index', '--find-links', store, '-c', constraints, 'ansible-core')
        call(out, *pip, '--python', new / 'bin/python', 'install', '--no-index', '--no-deps', wheel)
    if had != pins:
        retried(out, new / 'bin/ansible-galaxy', 'collection', 'install', '-r', INFRA / 'ansible/requirements.yml',
                '-p', ROOT / 'collections')
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
            build_wheel(out)
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


def install_prereqs():
    step('проверяю локальные зависимости')
    commands = ('ssh', 'sshpass', 'curl', 'python3', 'openssl')
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
    ensure_venv()
    os.environ['VPN2_WHEEL'] = str(wheel_path())


def dedupe(path):
    lines = list(dict.fromkeys(line for line in path.read_text().splitlines() if line.split()))
    path.with_name(f'{path.name}.u').write_text(''.join(f'{line}\n' for line in lines))
    os.replace(path.with_name(f'{path.name}.u'), path)
    path.chmod(0o600)


def prep_keys():
    """Ключ оркестратора: им entry ходит на экзиты после 00-bootstrap; копия в config.yaml
    переживает пересоздание entry. Ключ entry для ssh на экзиты (mgmt) - отдельный."""
    ssh, out = Path.home() / '.ssh', INFRA / 'secrets/rendered'
    for folder in (ssh, out):
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
    key, pub = ssh / 'id_ed25519', ssh / 'id_ed25519.pub'
    if not key.is_file():
        log('keypair оркестратора отсутствует — генерю ed25519')
        subprocess.run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-C', 'anysda-orchestrator', '-f', key, '-q'], check=True)
    if not pub.is_file():
        pub.write_text(subprocess.run(['ssh-keygen', '-y', '-f', key], check=True, capture_output=True, text=True).stdout)
    if CONFIG.is_file() and not re.search(r'(?m)^orchestrator_key:', CONFIG.read_text()):
        log('сохраняю orchestrator-ключ в config.yaml (переживёт пересоздание entry)')
        with open(CONFIG, 'a') as config:
            config.write('\n# orchestrator SSH-ключ — НЕ удалять: нужен для повторного деплоя\n'
                         '# с пересозданной entry-ноды. config.yaml надо сохранять/переносить.\n'
                         f'orchestrator_key: {base64.b64encode(key.read_bytes()).decode()}\n'
                         f'orchestrator_pubkey: {pub.read_text().rstrip(chr(10))}\n')
    authorized = ssh / 'authorized_keys'
    with open(authorized, 'a') as keys:
        keys.write(pub.read_text())
    (out / 'orchestrator_keys').write_text(''.join(read_raw(path) for path in (pub, ssh / 'id_rsa.pub', authorized)))
    for path in (authorized, out / 'orchestrator_keys'):
        dedupe(path)
    mgmt = Path('/root/.ssh/id_ed25519_mgmt')
    if not mgmt.is_file():
        subprocess.run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-q', '-C', 'anysda-entry-mgmt', '-f', mgmt], check=True)
    shutil.copyfile(f'{mgmt}.pub', out / 'entry_mgmt_key')
    (out / 'entry_mgmt_key').chmod(0o600)


def read_raw(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ''
