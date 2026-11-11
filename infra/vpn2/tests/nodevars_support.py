"""Оракул для переменных узла: env-файлы config2env.py, прочитанные bash, как их читает load_env."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASH_OWN = {'PWD', 'OLDPWD', 'SHLVL', '_'}


def sourced(*files):
    """Окружение после `set -a; source ...` в пустом bash: имя -> значение байт в байт."""
    script = 'set -a; for f in "$@"; do source "$f" || exit 1; done; set +a; unset f; exec env -0'
    out = subprocess.run(['env', '-i', 'bash', '--norc', '--noprofile', '-c', script, '_', *map(str, files)],
                         capture_output=True, check=True, timeout=30).stdout.decode('utf-8')
    pairs = dict(item.split('=', 1) for item in out.split('\0') if item)
    return {name: value for name, value in pairs.items() if name not in BASH_OWN}


def seed(directory, files):
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in (files or {}).items():
        (directory / name).write_text(body, encoding='utf-8')


def layout(tmp, text, *, state=None, legacy=None):
    """Каталог репы и каталог состояния под один прогон; возвращает (repo, state)."""
    repo, state_dir = Path(tmp) / 'repo', Path(tmp) / 'state'
    repo.mkdir(parents=True)
    (repo / 'config.yaml').write_text(text, encoding='utf-8')
    if state is not None:
        seed(state_dir, state)
    if legacy is not None:
        seed(repo / 'infra/state', legacy)
    return repo, state_dir


def old_env(repo, state_dir, *, development=False):
    """{узел: окружение} по config2env.py или None, если он отверг конфиг."""
    env = {'PATH': os.environ['PATH'], 'HOME': str(repo.parent / 'home'), 'VPN2_STATE_DIR': str(state_dir)}
    if development:
        env['VPN2_DEVELOPMENT'] = 'true'
    result = subprocess.run([sys.executable, str(ROOT / 'infra/lib/config2env.py'), str(repo)],
                            env=env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        return None
    envs = repo / 'infra/envs'
    return {path.stem: sourced(envs / 'all.env', path)
            for path in sorted(envs.glob('*.env')) if path.stem not in ('all', 'exits')}


def differences(old, new):
    """Список (узел, имя, старое, новое) по всем узлам и именам; отсутствие - None."""
    out = []
    for node in sorted(old.keys() | new.keys()):
        left, right = old.get(node), new.get(node)
        if left is None or right is None:
            out.append((node, '<узел>', left is not None, right is not None))
            continue
        for name in sorted(left.keys() | right.keys()):
            if left.get(name) != right.get(name) or type(left.get(name)) is not type(right.get(name)):
                out.append((node, name, left.get(name), right.get(name)))
    return out
