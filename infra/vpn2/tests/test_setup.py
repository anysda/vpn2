"""Мастер setup.sh: эталон прежнего bash-мастера и отказные ветки.

Каталог fixtures/setup/<сценарий> записан со старого setup.sh: ответы, ответы
подставных ip/curl/dig, весь вывод и итоговый config.yaml (expected.yaml: имя
config.yaml в репе не хранится). Новый мастер обязан повторить их байт в байт. Тест идёт и на Python 3.11 - старшей системной версии
Debian 12, поэтому берёт из пакета только vpn2.setup.
"""
import os
import pty
import select
import shutil
import signal
import subprocess
import termios
import time

import pytest

from setup_support import FIXTURES, ROOT, environment, node_env, normalize, prepare, run, run_tty
from vpn2 import setup

SCENARIOS = sorted(path.name for path in FIXTURES.iterdir() if (path / 'output').exists())
MINIMAL = (FIXTURES / 'minimal/answers').read_text()
MINIMAL_STUBS = {'ipify': '192.0.2.1', 'geo-198.51.100.2': '{}'}


def recorded(name, file):
    return (FIXTURES / name / file).read_text()


def scenario(tmp_path, name):
    stubs = FIXTURES / name / 'stubs'
    answers = {path.name: path.read_text() for path in stubs.iterdir()} if stubs.is_dir() else {}
    root = prepare(tmp_path, stubs=answers)
    before = FIXTURES / name / 'before.yaml'
    if before.exists():
        shutil.copy(before, root / 'config.yaml')
    return root


@pytest.mark.parametrize('name', SCENARIOS)
def test_matches_bash_wizard(tmp_path, name):
    root = scenario(tmp_path, name)
    code, output = run(root, recorded(name, 'answers'))
    assert code == 0, output
    assert normalize(output, root) == recorded(name, 'output')
    config = root / 'config.yaml'
    if name != 'cancel':
        assert normalize(config.read_text(), root) == recorded(name, 'expected.yaml')
        assert config.stat().st_mode & 0o777 == 0o600
    else:
        assert not config.exists()


def test_scenarios_cover_every_branch():
    assert SCENARIOS == ['branches', 'cancel', 'full', 'manual-entry', 'minimal']


def test_colors_on_terminal_match_bash_wizard(tmp_path):
    root = scenario(tmp_path, 'colors')
    code, stdout, stderr = run_tty(root, recorded('colors', 'answers'))
    assert code == 0, stderr
    # Терминал дописывает \r к каждому переводу строки, эталон хранится без него.
    assert '\n' in stdout and stdout.count('\r\n') == stdout.count('\n')
    assert normalize(stdout.replace('\r\n', '\n'), root) == recorded('colors', 'stdout')
    assert normalize(stderr, root) == recorded('colors', 'stderr')
    assert normalize((root / 'config.yaml').read_text(), root) == recorded('colors', 'expected.yaml')


def test_save_and_repeat(tmp_path):
    root = prepare(tmp_path, stubs=MINIMAL_STUBS)
    for _ in range(2):
        code, output = run(root, MINIMAL)
        assert code == 0, output
        assert 'Выход de: 198.51.100.2' in output
        assert (root / 'config.yaml').stat().st_mode & 0o777 == 0o600
        assert node_env(root)['EXIT_TAGS'] == 'de'


@pytest.mark.skipif(os.geteuid() == 0, reason='от root мастер ставит пакеты сам')
def test_missing_package_without_root_stops_before_questions(tmp_path):
    root = prepare(tmp_path, stubs=MINIMAL_STUBS)
    tools = tmp_path / 'tools'
    tools.mkdir()
    for name in ('bash', 'dirname', 'python3', 'cat'):
        (tools / name).symlink_to(shutil.which(name))
    (tmp_path / 'bin/dig').unlink()
    env = {**environment(root), 'PATH': f'{tmp_path}/bin:{tools}'}
    done = subprocess.run([tools / 'bash', root / 'setup.sh'], cwd=root, env=env, input=MINIMAL,
                          capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 1
    assert done.stderr == '  ✗ До запуска мастера установите пакеты: bind9-dnsutils\n'
    assert done.stdout == ''
    assert not (root / 'config.yaml').exists()


@pytest.mark.parametrize('answers, message', [
    ('', '✗ EOF: Количество — ввод прерван'),
    ('1\n', '✗ EOF: root password — ввод прерван'),
    ('1\np\n198.51.100.2\np\n', '✗ EOF: Тег ноды (us / de / se / ...) — ввод прерван'),
    ('1\np\n198.51.100.2\np\nde', '✗ EOF: Тег ноды (us / de / se / ...) — ввод прерван'),
    ('1\np\n198.51.100.2\np\nde\n\n', None),
    ('1\np\n198.51.100.2\np\nde\n\nlongpassword\n', None),
    ('1\np\n198.51.100.2\np\nde\n\n\n', None),
    ('1\np\n198.51.100.2\np\nde\n\n\n\nn\nn\ny\ny\nlocal\n', None),
    (MINIMAL[:-2], '✗ EOF: Сохранить config.yaml? (y/n) — ввод прерван'),
])
def test_input_end_aborts_without_config(tmp_path, answers, message):
    root = prepare(tmp_path, stubs=MINIMAL_STUBS)
    code, output = run(root, answers)
    assert code == 1, output
    assert (message in output) if message else ('EOF' not in output)
    assert 'Traceback' not in output
    assert list(root.glob('*.yaml')) == []


def test_dig_failure_reads_as_unresolved_domain(tmp_path):
    root = prepare(tmp_path, stubs={**MINIMAL_STUBS, 'dig-fail': ''})
    answers = ['1', 'p', '198.51.100.2', 'p', 'de', '', '', '',
               'y', 'vpn.example.com', 'y', 'n', 'n', 'n', 'y']
    code, output = run(root, '\n'.join(answers) + '\n')
    assert code == 0, output
    assert 'проверяю DNS...   ! DNS не разрешает домен' in output
    assert "  domain: 'vpn.example.com'\n" in (root / 'config.yaml').read_text()


def test_generated_passphrase_is_not_printed(tmp_path):
    root = prepare(tmp_path, stubs=MINIMAL_STUBS)
    answers = ['1', 'p', '198.51.100.2', 'p', 'de', '', '', '', 'n', 'n', 'y', 'y', '', '', 'n', 'y']
    code, output = run(root, '\n'.join(answers) + '\n')
    assert code == 0, output
    line = next(line for line in (root / 'config.yaml').read_text().splitlines()
                if line.startswith('  passphrase: '))
    passphrase = line.split("'")[1]
    assert len(passphrase) == 40 and passphrase.isalnum()
    assert passphrase not in output
    assert 'он будет в config.yaml (chmod 600)' in output


class Recorder:
    def __init__(self):
        self.lines = []

    def fail(self, text):
        self.lines.append(text)


@pytest.mark.parametrize('tag', ['a', 'us2', 'us-2', 'us--2'])
def test_exit_tag_accepted(tag):
    ui = Recorder()
    assert setup.validate_tag(ui, tag, []) and ui.lines == []


@pytest.mark.parametrize('tag, used, reason', [
    ('-us', [], 'дефис в конце запрещён'), ('us-', [], 'дефис в конце запрещён'),
    ('US', [], 'строчная буква в начале'), ('us_2', [], 'строчные буквы, цифры и дефисы'),
    ('', [], 'строчная буква в начале'), ('us\n', [], 'строчная буква в начале'),
    ('ru', [], "Тег 'ru' зарезервирован"), ('all', [], 'зарезервирован'),
    ('exits', [], 'зарезервирован'), ('foreign', [], 'зарезервирован'),
    ('us-2', ['se', 'us-2', 'us2'], "Тег 'us-2' уже используется"),
])
def test_exit_tag_rejected(tag, used, reason):
    ui = Recorder()
    assert not setup.validate_tag(ui, tag, used)
    assert len(ui.lines) == 1 and reason in ui.lines[0]


@pytest.mark.parametrize('existing', [False, True])
def test_config_is_private_before_write(tmp_path, monkeypatch, existing):
    (tmp_path / 'infra').symlink_to(ROOT / 'infra')
    target = tmp_path / 'config.yaml'
    if existing:
        target.write_text("youtube:\n  route: 'off'\n")
        target.chmod(0o644)
    written = []
    original = os.fdopen

    def private_fdopen(*args, **kwargs):
        stream = original(*args, **kwargs)
        write = stream.write

        def private_write(data):
            assert os.fstat(stream.fileno()).st_mode & 0o777 == 0o600
            written.append(data)
            return write(data)

        stream.write = private_write
        return stream

    monkeypatch.setattr(setup, 'ROOT', tmp_path)
    monkeypatch.setattr(os, 'fdopen', private_fdopen)
    os.umask(0o022)
    answers = setup.defaults()
    answers['entry'] = {'host': '192.0.2.1', 'password': 'CANARY_SECRET'}
    answers['exits'] = [('de', '192.0.2.2', 'CANARY_SECRET')]
    answers['admin']['password'] = 'CANARY_SECRET'
    setup.save(setup.render(answers))
    assert len(written) == 1
    assert target.read_text().count('CANARY_SECRET') == 3
    assert target.stat().st_mode & 0o777 == 0o600
    assert [path.name for path in tmp_path.glob('.config-*')] == []


def wait_for(stream, text, seen=b''):
    deadline = time.monotonic() + 10
    while text.encode() not in seen:
        assert time.monotonic() < deadline, f'нет вопроса «{text}»: {seen.decode()}'
        if select.select([stream], [], [], 0.1)[0]:
            seen += os.read(stream.fileno(), 65536)
    return seen


@pytest.mark.parametrize('number', [signal.SIGINT, signal.SIGTERM])
def test_signal_restores_terminal_and_keeps_config(tmp_path, number):
    root = prepare(tmp_path, stubs=MINIMAL_STUBS)
    (root / 'config.yaml').write_text('# прежний\n')
    master, slave = pty.openpty()
    try:
        with subprocess.Popen(['bash', str(root / 'setup.sh')], cwd=root, env=environment(root),
                              stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              start_new_session=True) as process:
            try:
                seen = wait_for(process.stdout, 'Количество')
                os.write(master, b'1\n')
                wait_for(process.stdout, 'root password', seen[seen.index(b'\xd0\x9a\xd0\xbe\xd0\xbb'):])
                deadline = time.monotonic() + 10
                while termios.tcgetattr(slave)[3] & termios.ECHO:
                    assert time.monotonic() < deadline, 'пароль спрашивается при включённом ECHO'
                    time.sleep(0.01)
                os.kill(process.pid, number)
                output = process.communicate(timeout=10)[0].decode()
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
        assert process.returncode == -number, output
        assert termios.tcgetattr(slave)[3] & termios.ECHO
        assert 'Traceback' not in output
        assert (root / 'config.yaml').read_text() == '# прежний\n'
    finally:
        os.close(master)
        os.close(slave)
