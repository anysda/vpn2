"""./deploy.sh: договор с оператором и порядок плеев. ansible-playbook - заглушка, пишет аргументы."""
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CONFIG = (ROOT / 'config.example.yaml').read_text().replace('  passphrase: ""', '  passphrase: test pass')
TREE = ('deploy.sh', 'infra/deploy.sh', 'infra/lib', 'infra/ansible', 'infra/vpn2/src', 'infra/vpn2/pyproject.toml')
# Заглушка ansible-playbook: строка JSON на вызов; FAKE_RULES - [подстрока аргументов, код, пауза].
PLAYBOOK = r'''#!PYTHON
import json, os, sys, time
args, argv = ' '.join(sys.argv[1:]), sys.argv[1:]
with open(os.environ['FAKE_PLAYS'], 'a') as out:
    out.write(json.dumps({'args': args, 'follows': os.environ.get('VPN2_PANEL_FOLLOWS'), 'pid': os.getpid()}) + '\n')
extra = dict(argv[i + 1].split('=', 1) for i, a in enumerate(argv) if a == '-e')
hosts = argv[argv.index('--limit') + 1].split(',') if '--limit' in argv else []
for host in hosts if 'prepare,99-verify' in args else ():
    open(f"{os.environ['VPN2_VERIFY_OUT']}/{host}", 'w').write(f'0\n[{host}] ✓ всё измерено\n')
for host in hosts if '--tags secrets' in args else ():
    upper = host.upper()
    open(f"{extra['out']}/{host}.env", 'w').write(''.join(f'{upper}_{k}=x\n' for k in ('DIRECT', 'WARP', 'OBFS', 'MGMT', 'CLASH')))
    for kind in ('pem', 'pub', 'reality'):
        open(f"{extra['out']}/{host}.{kind}", 'w').close()
for match, code, pause in json.loads(os.environ.get('FAKE_RULES', '[]')):
    if match in args:
        time.sleep(pause)
        sys.exit(code)
'''
RUNNER = '''import sys
from vpn2.deploy import __main__ as main, run
main.install_prereqs = run.prep_keys = lambda: None
run.svc = lambda *_args, **_options: 0
sys.exit(main.main(sys.argv[1:]))
'''


@pytest.fixture
def tree(tmp_path):
    return make_tree(tmp_path)


def make_tree(tmp_path):
    """Копия дерева с config.yaml и venv из заглушек."""
    for name in TREE:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        copy = shutil.copytree if (ROOT / name).is_dir() else shutil.copy2
        copy(ROOT / name, tmp_path / name, **({'ignore': shutil.ignore_patterns('__pycache__')} if copy is shutil.copytree else {}))
    (tmp_path / 'config.yaml').write_text(CONFIG)
    venv = tmp_path / 'root/venv/bin'
    venv.mkdir(parents=True)
    (venv / 'ansible-playbook').write_text(PLAYBOOK.replace('PYTHON', sys.executable))
    (venv / 'ansible').write_text('#!/bin/sh\nexit 4\n')
    for fake in venv.iterdir():
        fake.chmod(0o755)
    return tmp_path


def environ(tree, **extra):
    env = {**os.environ, 'HOME': str(tree / 'home'), 'VPN2_STATE_DIR': str(tree / 'state'), 'VPN2_DEPLOY_LOCK': str(tree / 'lock'),
           'VPN2_ROOT': str(tree / 'root'), 'FAKE_PLAYS': str(tree / 'plays'), 'PYTHONPATH': str(tree / 'infra/vpn2/src'),
           'DEPLOY_ROOT': str(tree / 'infra'), 'VPN2_DEVELOPMENT': 'true', **extra}
    for name in ('VPN2_RELEASE_MANIFEST', 'VPN2_PANEL_FOLLOWS', 'VPN2_VERIFY_OUT'):
        env.pop(name, None)
    return env


def deploy(tree, *args, **extra):
    """Настоящий ./deploy.sh копии дерева: отказы до установки зависимостей."""
    return subprocess.run(['bash', tree / 'deploy.sh', *args], input='', capture_output=True, text=True, timeout=60,
                          env=environ(tree, **extra))


def orchestrate(tree, *args, wait=True, **extra):
    """Оркестратор пакета без apt, venv, ключей и правок entry; плеи - заглушка."""
    command = [sys.executable, '-B', '-c', RUNNER, *args]
    if not wait:
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environ(tree, **extra))
    return subprocess.run(command, capture_output=True, text=True, timeout=120, env=environ(tree, **extra))


def alive(pid):
    """Процесс жив через 3 с после выхода деплоя (зомби, которого ещё не собрал init, - не жив)."""
    for _ in range(30):
        try:
            if Path(f'/proc/{pid}/stat').read_text().split(') ')[1][0] == 'Z':
                return False
        except (OSError, IndexError):
            return False
        time.sleep(0.1)
    return True


def plays(tree):
    path = tree / 'plays'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_help_needs_no_config_lock_or_root(tree):
    (tree / 'config.yaml').unlink()
    for args in (['--help'], ['-h'], ['--development', '--help']):
        done = deploy(tree, *args, VPN2_STATE_DIR='/proc/denied', VPN2_DEPLOY_LOCK='/proc/denied/lock')
        assert done.returncode == 0, done.stderr
        assert done.stdout.startswith('anysda-vpn2 — деплой\n')
        assert 'Группы:   ru | foreign | all | <конкретный тег экзита>' in done.stdout


def test_missing_config_is_refused(tree):
    (tree / 'config.yaml').unlink()
    done = deploy(tree)
    assert done.returncode == 1 and 'config.yaml не найден' in done.stdout and not plays(tree)


def test_second_deploy_on_the_machine_is_refused(tree):
    with open(tree / 'lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        done = deploy(tree, '--development', '30-frontend', 'ru')
    assert done.returncode == 1
    assert 'deploy уже выполняется на этой машине' in done.stderr and not plays(tree)


@pytest.mark.parametrize('args, said', [
    (['30-frontend'], 'использование: ./deploy.sh <stage> <group>'),
    (['nope', 'ru'], 'нет такой стадии: nope'),
    (['30-frontend', 'xx'], 'unknown host group: xx'),
    (['30-frontend', 'us'], '30-frontend - стадия entry, на экзитах её нет'),
    (['10-foreign', 'ru'], '10-foreign - стадия экзитов, на entry её нет'),
])
def test_wrong_arguments_are_refused_before_any_change(tree, args, said):
    env = {name: value for name, value in environ(tree).items() if name != 'HOME'}
    # Запуск из systemd: ни HOME, ни tty.
    done = subprocess.run(['bash', tree / 'deploy.sh', '--development', *args], capture_output=True, text=True,
                          timeout=60, env=env, stdin=subprocess.DEVNULL)
    assert done.returncode == 1, done.stderr
    assert said in done.stderr and 'FATAL' in done.stderr and 'HOME' not in done.stderr
    assert not plays(tree)


def test_manifest_file_must_exist(tree):
    done = deploy(tree, '--manifest', tree / 'missing.json')
    assert done.returncode == 1 and 'файл манифеста не найден' in done.stderr


def test_one_stage_runs_only_its_play_on_its_nodes(tree):
    done = orchestrate(tree, '30-frontend', 'ru')
    assert done.returncode == 0, done.stderr
    calls = [call['args'] for call in plays(tree)]
    staged = [args for args in calls if 'site.yml' in args]
    assert len(staged) == 1 and '--tags prepare,30-frontend --limit ru ' in staged[0] and 'guard=True' in staged[0]
    tags = [args.split('--tags ')[1].split()[0] for args in calls if 'site.yml' not in args]
    assert tags == ['journal', 'release', 'beat', 'finish', 'complete']
    assert '[deploy] [30-frontend → ru] done' in done.stderr


def test_full_deploy_follows_site_yml(tree):
    done = orchestrate(tree)
    assert done.returncode == 0, done.stderr
    calls = plays(tree)
    staged = [(call['args'].split('--tags prepare,')[1].split()[0], call['args'].split('--limit ')[1].split()[0], call['follows'])
              for call in calls if '--tags prepare,' in call['args']]
    assert staged == [
        ('00-bootstrap', 'ru,us,gb', None), ('05-mgmt-mesh', 'ru,us,gb', None),
        ('10-foreign', 'us', None), ('99-verify', 'us', None), ('10-foreign', 'gb', None), ('99-verify', 'gb', None),
        ('28-wireguard', 'ru', None), ('29-openvpn', 'ru', None), ('19-yt-zapret', 'ru', None), ('20-ru-router', 'ru', None),
        ('21-failover-watchdog', 'ru', None), ('22-adguard', 'ru', None), ('23-emergency', 'ru,us,gb', None),
        ('25-monitoring', 'ru', None), ('26-backup', 'ru', None), ('27-ikev2', 'ru', None), ('29-control', 'ru', None),
        ('35-telegram', 'ru', 'true'), ('30-frontend', 'ru', None), ('99-verify', 'ru,us,gb', None)]
    assert [call['args'].split('--tags ')[1].split()[0] for call in calls][:3] == ['journal', 'release', 'preflight']
    assert '--tags old-layout' in calls[-2]['args'] and '--tags complete' in calls[-1]['args']
    assert 'деплой завершён' in done.stdout and re.search(r'│  30-frontend +ru +\d+s +│', done.stdout)


def test_recover_skips_the_journal_check(tree):
    done = orchestrate(tree, 'recover', FAKE_RULES=json.dumps([['--tags journal', 2, 0]]))
    assert done.returncode == 0, done.stderr
    assert not [call for call in plays(tree) if '--tags journal' in call['args']]


def test_unfinished_deploy_is_refused_without_changes(tree):
    done = orchestrate(tree, FAKE_RULES=json.dumps([['--tags journal', 2, 0]]))
    assert done.returncode == 1 and 'журнал деплоя на узлах не прочитан' in done.stderr
    assert [call['args'].split('--tags ')[1].split()[0] for call in plays(tree)] == ['journal']


def test_failed_release_checks_close_the_journal(tree):
    done = orchestrate(tree, FAKE_RULES=json.dumps([['--tags release', 2, 0]]))
    assert done.returncode == 1
    assert [call['args'].split('--tags ')[1].split()[0] for call in plays(tree)] == ['journal', 'release', 'abort']


def test_failed_stage_stops_the_deploy_and_keeps_the_journal(tree):
    done = orchestrate(tree, FAKE_RULES=json.dumps([['prepare,20-ru-router', 2, 0]]))
    assert done.returncode == 1
    assert 'стадия 20-ru-router: одна или несколько нод завершились с ошибкой' in done.stderr
    assert 'deploy interrupted: operation journal retained' in done.stderr
    assert not [call for call in plays(tree) if 'prepare,21-' in call['args'] or '--tags abort' in call['args']]


@pytest.mark.parametrize('sig, code', [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_killed_in_firewall_stage(tree, sig, code):
    """Сигнал посреди стадии с фаерволом: плей стадии и пульс гаснут, журнал узлов остаётся для recover."""
    proc = orchestrate(tree, wait=False, FAKE_RULES=json.dumps([['prepare,05-mgmt-mesh', 0, 60]]))
    for _ in range(300):
        if any('prepare,05-mgmt-mesh' in call['args'] for call in plays(tree)):
            break
        time.sleep(0.1)
    time.sleep(6)  # пульс сторожа успел начаться
    proc.send_signal(sig)
    _out, err = proc.communicate(timeout=30)
    assert proc.returncode == code, err
    assert 'deploy interrupted: operation journal retained' in err
    staged = next(call for call in plays(tree) if 'prepare,05-mgmt-mesh' in call['args'])
    assert not alive(staged['pid'])
    assert [call for call in plays(tree) if '--tags beat' in call['args'] and 'entry_outside' in call['args']]
    assert not [call for call in plays(tree) if 'stage=05-mgmt-mesh' in call['args'] and '--tags finish' in call['args']
                or '--tags abort' in call['args']]
