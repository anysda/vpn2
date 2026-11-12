"""Запуск мастера setup.sh в копии репы с подставными ip, curl и dig.

Сеть и пакеты машины теста не трогаются: команды мастера берутся из каталога
bin, ответы им лежат файлами в каталоге stubs:
  ip-route, ip-addr - вывод `ip -4 route show default` и `ip -4 addr show`;
  ipify, ifconfig   - внешний адрес (нет файла = сервис недоступен);
  geo-<адрес>       - ответ ip-api.com (нет файла = недоступен);
  dig-<домен>       - A-запись (нет файла = пустой ответ);
  dig-fail          - dig завершается с ошибкой.
"""
import os
import pty
import re
import shutil
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / 'fixtures/setup'

COPIED = ('setup.sh', 'infra/ansible/inventory.py',
          'infra/vpn2/src/vpn2/__init__.py', 'infra/vpn2/src/vpn2/setup.py', 'infra/vpn2/src/vpn2/config.py',
          'infra/vpn2/src/vpn2/fsutil.py',
          'infra/vpn2/src/vpn2/nodevars.py', 'infra/vpn2/src/vpn2/watchdog/__init__.py',
          'infra/vpn2/src/vpn2/watchdog/hy2_bandwidth.py')



def node_env(root, node='ru'):
    """Переменные узла из записанного config.yaml - то, что деплой отдаёт ролям инвентарём."""
    from vpn2 import config, nodevars
    cfg = config.loads((root / 'config.yaml').read_text())
    return nodevars.env_by_node(cfg, state=root / 'state', repo_root=root)[node]


STUBS = {
    'ip': '''case "$*" in *route*) f=ip-route ;; *) f=ip-addr ;; esac
[ -f "$STUBS/$f" ] && cat "$STUBS/$f"
exit 0
''',
    'curl': '''for url; do :; done
case "$url" in
  *ip-api.com/json/*) f=${url#*json/}; f=geo-${f%%\\?*} ;;
  *api.ipify.org*) f=ipify ;;
  *ifconfig.me*) f=ifconfig ;;
  *) exit 22 ;;
esac
[ -f "$STUBS/$f" ] || exit 22
cat "$STUBS/$f"
''',
    'dig': '''[ -f "$STUBS/dig-fail" ] && exit 9
[ -f "$STUBS/dig-$4" ] && cat "$STUBS/dig-$4"
exit 0
''',
}

STAMP = re.compile(r'(# Создано \./setup\.sh) \d{4}-\d\d-\d\d \d\d:\d\d')
GENERATED = re.compile(r"(passphrase: )'[A-Za-z0-9]{40}'")


def prepare(directory, source=ROOT, stubs=None):
    """Копия репы в directory/repo, команды в directory/bin, ответы в directory/stubs."""
    base = Path(directory)
    root = base / 'repo'
    for name in COPIED:
        if (source / name).exists():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source / name, root / name)
    (base / 'bin').mkdir()
    for name, body in STUBS.items():
        path = base / 'bin' / name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)
    (base / 'stubs').mkdir()
    for name, text in (stubs or {}).items():
        (base / 'stubs' / name).write_text(text)
    return root


def environment(root):
    base = root.parent
    return {**os.environ, 'PATH': f'{base}/bin:' + os.environ['PATH'], 'STUBS': f'{base}/stubs',
            'HOME': str(base / 'home'), 'VPN2_STATE_DIR': str(base / 'state')}


def run(root, answers, args=(), timeout=20):
    """(код, общий вывод stdout+stderr); answers - bytes или str."""
    if isinstance(answers, str):
        answers = answers.encode()
    with subprocess.Popen(['bash', str(root / 'setup.sh'), *args], cwd=root, env=environment(root),
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True) as process:
        try:
            output, _ = process.communicate(answers, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise AssertionError('мастер не завершился') from None
        return process.returncode, output.decode('utf-8', 'surrogateescape')


def run_tty(root, answers, timeout=20):
    """(код, stdout, stderr) при stdout на терминале: мастер включает цвета."""
    master, slave = pty.openpty()
    try:
        with subprocess.Popen(['bash', str(root / 'setup.sh')], cwd=root, env=environment(root),
                              stdin=subprocess.PIPE, stdout=slave, stderr=subprocess.PIPE,
                              start_new_session=True) as process:
            os.close(slave)
            slave = None
            _, stderr = process.communicate(answers.encode(), timeout=timeout)
            stdout = b''
            while True:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                stdout += chunk
            return process.returncode, stdout.decode(), stderr.decode()
    finally:
        os.close(master)
        if slave is not None:
            os.close(slave)


def normalize(text, root):
    """Убирает из вывода и конфига то, что меняется от запуска к запуску."""
    text = text.replace(str(root), '<ROOT>')
    return GENERATED.sub(r"\1'<GENERATED>'", STAMP.sub(r'\1 <STAMP>', text))
