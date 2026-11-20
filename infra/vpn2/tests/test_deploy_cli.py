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
    out.write(json.dumps({'args': args, 'follows': os.environ.get('VPN2_PANEL_FOLLOWS'), 'pid': os.getpid(), 't': time.time()}) + '\n')
extra = dict(argv[i + 1].split('=', 1) for i, a in enumerate(argv) if a == '-e' and '=' in argv[i + 1])
hosts = argv[argv.index('--limit') + 1].split(',') if '--limit' in argv else []
for host in hosts if '99-verify --limit' in args else ():
    open(f"{os.environ['VPN2_VERIFY_OUT']}/{host}", 'w').write(f'0\n[{host}] ✓ всё измерено\n')
for host in hosts if '--tags secrets' in args else ():
    upper = host.upper()
    open(f"{extra['out']}/{host}.env", 'w').write(''.join(f'{upper}_{k}=x\n' for k in ('DIRECT', 'WARP', 'OBFS', 'MGMT', 'CLASH')))
    for kind in ('pem', 'pub', 'reality'):
        open(f"{extra['out']}/{host}.{kind}", 'w').close()
if os.environ.get('FAKE_OPEN') and '--tags release' in args and 'journal=True' in args:
    open(f"{extra['out']}/ru.journal", 'w').write('unfinished deploy on ru: operation journal is open\n')
    sys.exit(2)
for match, code, pause in json.loads(os.environ.get('FAKE_RULES', '[]')):
    if match in args:
        time.sleep(pause)
        sys.exit(code)
'''
# Заглушка ssh: строка JSON на вход; FAKE_SSH_RULES - [подстрока «адрес команда», код, stderr].
SSH = r'''#!PYTHON
import json, os, sys
host, command = sys.argv[-2].split('@')[1], sys.argv[-1]
with open(os.environ['FAKE_SSH'], 'a') as out:
    out.write(json.dumps({'host': host, 'command': command}) + '\n')
rule = next((rule for rule in json.loads(os.environ.get('FAKE_SSH_RULES', '[]')) if rule[0] in f'{host} {command}'), None)
sys.stderr.write(rule[2] if rule and len(rule) > 2 else '')
sys.exit(rule[1] if rule else 0)
'''
RUNNER = '''import sys
from vpn2.deploy import __main__ as main, run
main.install_prereqs = run.prep_keys = lambda: None
main.svc = lambda *_args, **_options: 0
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
    (venv / 'python3').symlink_to(sys.executable)
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'bin/ssh').write_text(SSH.replace('PYTHON', sys.executable))
    for fake in (venv / 'ansible-playbook', venv / 'ansible', *(tmp_path / 'bin').iterdir()):
        fake.chmod(0o755)
    return tmp_path


def environ(tree, **extra):
    env = {**os.environ, 'HOME': str(tree / 'home'), 'VPN2_STATE_DIR': str(tree / 'state'), 'VPN2_DEPLOY_LOCK': str(tree / 'lock'),
           'VPN2_ROOT': str(tree / 'root'), 'FAKE_PLAYS': str(tree / 'plays'), 'FAKE_SSH': str(tree / 'ssh'),
           'PATH': f"{tree / 'bin'}:{os.environ['PATH']}", 'PYTHONPATH': str(tree / 'infra/vpn2/src'),
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


@pytest.fixture
def inproc(tree, capfd, monkeypatch):
    """Оркестратор в этом процессе (покрытие): пути модулей из окружения копии дерева, после - как были."""
    import importlib

    from vpn2 import config, deploy
    from vpn2.deploy import __main__ as main, report, run
    saved, handlers = dict(os.environ), {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    services = []

    def reload():
        for module in (deploy, report, run, main):
            importlib.reload(module)

    def call(*args, **extra):
        env = environ(tree, **extra)
        os.environ.clear()
        os.environ.update(env)
        monkeypatch.setattr(config, 'STATE_DIR', tree / 'state')
        reload()
        main.install_prereqs = run.prep_keys = lambda: None
        main.svc = lambda *args, **_options: services.append(args) or 0
        capfd.readouterr()
        code = main.main(list(args))
        out, err = capfd.readouterr()
        return subprocess.CompletedProcess(args, code, out, err)
    call.services = services
    yield call
    monkeypatch.undo()
    os.environ.clear()
    os.environ.update(saved)
    for sig, handler in handlers.items():
        signal.signal(sig, handler)
    reload()


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


def plays(tree, name='plays'):
    path = tree / name
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_help_needs_no_config_lock_or_root(tree):
    (tree / 'config.yaml').unlink()
    for args in (['--help'], ['-h'], ['--development', '--help']):
        done = deploy(tree, *args, VPN2_STATE_DIR='/proc/denied', VPN2_DEPLOY_LOCK='/proc/denied/lock')
        assert done.returncode == 0, done.stderr
        assert done.stdout.startswith('anysda-vpn2 — деплой\n')
        assert done.stdout == (ROOT / 'infra/vpn2/tests/fixtures/deploy-help.txt').read_text()


def test_clean_system_without_config_names_the_install_steps(tree):
    (tree / 'config.yaml').unlink()
    for args in ([], ['-yaml']):
        done = deploy(tree, *args)
        assert done.returncode == 1 and 'config.yaml не найден' in done.stdout and not plays(tree)
        assert './deploy.sh -key' in done.stdout and './deploy.sh -yaml' in done.stdout and 'setup.sh' not in done.stdout


def test_key_is_created_once_and_printed_the_same(tree):
    (tree / 'config.yaml').unlink()
    first, second = deploy(tree, '-key'), deploy(tree, '-key')
    assert first.returncode == second.returncode == 0, first.stderr
    assert first.stdout == second.stdout and first.stdout.startswith('ssh-ed25519 ') and first.stdout.count('\n') == 1
    # Тем же ключом деплой входит и на саму entry.
    assert (tree / 'home/.ssh/authorized_keys').read_text() == first.stdout
    assert '/root/.ssh/authorized_keys на каждом экзите' in first.stderr and not plays(tree, 'ssh')


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


def test_one_stage_runs_only_its_play_on_its_nodes(tree, inproc):
    done = inproc('30-frontend', 'ru')
    assert done.returncode == 0, done.stderr
    calls = [call['args'] for call in plays(tree)]
    assert [args.split('--tags ')[1].split()[0] for args in calls] == ['release', '30-frontend']
    assert '--limit ru ' in calls[1] and 'scope=ru ' in calls[0] and 'journal=True' in calls[0] and 'bootstrap=False' in calls[0]
    # Сторож, пульс, снятие и закрытие журнала - ssh на entry; уборка выкладки - на экзитах в конце.
    logins = [(call['host'], call['command']) for call in plays(tree, 'ssh')]
    ru = [command for host, command in logins if host == '1.2.3.4']
    assert ru[0].startswith('touch') and 'fwguard.py arm 30-frontend' in ru[1]
    assert 'disarm' in ru[-2] and 'state phase --phase complete' in ru[-1]
    assert {host for host, command in logins[-2:]} == {'5.6.7.8', '9.10.11.12'} and all('rm -rf' in c for _, c in logins[-2:])
    assert '[deploy] [30-frontend → ru] done' in done.stderr


def test_full_deploy_follows_site_yml(tree, inproc):
    together = ['21-failover-watchdog', '22-adguard', '25-monitoring', '26-backup']
    chained = ['27-ikev2', '29-control', '35-telegram']
    done = inproc(FAKE_RULES=json.dumps([['--tags 19-yt-zapret ', 0, 2], ['--tags 27-ikev2 ', 0, 2],
                                         *([f'--tags {name} ', 0, 1] for name in together)]))
    assert done.returncode == 0, done.stderr
    calls = plays(tree)
    staged = [(call['args'].split('--tags ')[1].split()[0], call['args'].split('--limit ')[1].split()[0], call['follows'])
              for call in calls if 'site.yml' in call['args']]
    # Экзиты идут своей цепочкой рядом со стадиями entry; общие стадии - на всех узлах сразу.
    exits = [item for item in staged if 'ru' not in item[1].split(',')]
    assert exits == [('10-foreign', 'us', None), ('99-verify', 'us', None), ('10-foreign', 'gb', None),
                     ('99-verify', 'gb', None), ('23-emergency', 'us,gb', None)]
    entry = [item for item in staged if item not in exits]
    tags = lambda call: call['args'].split('--tags ')[1].split()[0]  # noqa: E731
    assert entry[:7] + entry[14:] == [
        ('00-bootstrap', 'ru,us,gb', None), ('05-mgmt-mesh', 'ru,us,gb', None),
        ('28-wireguard', 'ru', None), ('29-openvpn', 'ru', None), ('19-yt-zapret', 'ru', None), ('20-ru-router', 'ru', None),
        ('23-emergency', 'ru', None), ('30-frontend', 'ru', None), ('99-verify', 'ru,us,gb', None)]
    # Стадии группы идут разом: каждая началась раньше, чем кончилась любая (пауза заглушки 1 с).
    assert sorted(entry[7:11]) == [(name, 'ru', None) for name in together]
    grouped = [call for call in calls if any(f'--tags {name} ' in call['args'] for name in together)]
    assert max(call['t'] for call in grouped) - min(call['t'] for call in grouped) < 0.9
    # Вторая группа: 27-ikev2 (пауза 2 с) рядом с цепочкой 29-control, 35-telegram.
    assert [item for item in entry[11:14] if item[0] != '27-ikev2'] == [('29-control', 'ru', None), ('35-telegram', 'ru', 'true')]
    begun = {tags(call): call['t'] for call in calls if any(f'--tags {name} ' in call['args'] for name in chained)}
    assert begun['35-telegram'] < begun['27-ikev2'] + 1.5
    grouped += [call for call in calls if any(f'--tags {name} ' in call['args'] for name in chained)]
    assert all('mitogen_task_isolation=fork' in call['args'] for call in grouped)
    assert not [call for call in calls if call not in grouped and 'mitogen_task_isolation' in call['args']]
    rows = [row.split()[1] for row in done.stdout.splitlines() if re.search(r'│  \d\d-', row)]
    assert [row for row in rows if row in together + chained] == together + chained
    # WARP экзитов полный деплой не ждёт: проверка своим юнитом, итог в итоговом 99-verify.
    assert [tags(call) for call in calls if '{"exit_warp_wait": false}' in call['args']] == ['10-foreign', '10-foreign']
    at = [item[0] for item in staged].index
    assert at('05-mgmt-mesh') < at('10-foreign') and staged.index(('99-verify', 'gb', None)) < at('20-ru-router')
    assert at('20-ru-router') < staged.index(('23-emergency', 'us,gb', None)) < at('30-frontend')
    # Секреты экзитов тянет цепочка экзитов за 10-foreign, один раз, пока entry ещё в 19-yt-zapret.
    pulled = [call['t'] for call in calls if '--tags secrets' in call['args']]
    zapret = next(call['t'] for call in calls if '--tags 19-yt-zapret ' in call['args'])
    assert len(pulled) == 1 and pulled[0] < zapret + 2
    assert [call['args'].split('--tags ')[1].split()[0] for call in calls][:2] == ['release', '00-bootstrap']
    # Вход по ключу проверен на всех узлах до первого плея: по ssh-входу на узел, ничего не запуская.
    assert sorted((call['host'], call['command']) for call in plays(tree, 'ssh')[:3]) == [
        ('1.2.3.4', 'true'), ('5.6.7.8', 'true'), ('9.10.11.12', 'true')]
    assert 'bootstrap=True' in calls[0]['args'] and 'scope= ' in calls[0]['args']
    # Остатки v1.1.5 убирает последняя 99-verify, а не проверки экзитов в 10-foreign.
    assert '--tags 99-verify --limit ru,us,gb' in calls[-1]['args'] and 'verify_old_layout=true' in calls[-1]['args']
    assert not [call for call in calls[:-1] if 'verify_old_layout' in call['args']]
    closing = [call['command'] for call in plays(tree, 'ssh') if 'state phase' in call['command']]
    assert len(closing) == 3
    assert 'деплой завершён' in done.stdout and re.search(r'│  30-frontend +ru +\d+s +│', done.stdout)


def test_exits_without_key_stop_the_deploy_before_any_change(tree, inproc):
    """Оба экзита без ключа оркестратора названы одним списком; ни плеев, ни шагов на entry."""
    done = inproc(FAKE_SSH_RULES=json.dumps([['5.6.7.8 true', 255, 'root@5.6.7.8: Permission denied (publickey).'],
                                             ['9.10.11.12 true', 255, 'root@9.10.11.12: Permission denied (publickey).']]))
    assert done.returncode == 1
    assert 'нода us (5.6.7.8) не пускает по ключу' in done.stdout and 'нода gb (9.10.11.12) не пускает по ключу' in done.stdout
    key = (tree / 'home/.ssh/id_ed25519.pub').read_text().strip()
    assert f'в /root/.ssh/authorized_keys есть строка из ./deploy.sh -key: {key}' in done.stdout and 'порт 22' in done.stdout
    assert 'узлов без входа по ключу: 2, на серверах ничего не менялось' in done.stderr
    assert not plays(tree) and not inproc.services and {call['command'] for call in plays(tree, 'ssh')} == {'true'}


def test_unreachable_exit_of_a_clean_install_is_refused(tree, inproc):
    done = inproc(PREFLIGHT_BUDGET='0', FAKE_SSH_RULES=json.dumps(
        [['9.10.11.12 true', 255, 'ssh: connect to host 9.10.11.12 port 22: Connection timed out']]))
    assert done.returncode == 1 and 'нода gb (9.10.11.12) не пускает по ключу' in done.stdout
    assert 'sshd на ноде запущен и слушает порт 22' in done.stdout and not plays(tree) and not inproc.services


def test_root_passwords_of_1_2_0_leave_config_after_a_good_deploy_only(tree, inproc):
    legacy = CONFIG.replace('  host: 1.2.3.4\n', '  host: 1.2.3.4\n  password: root pw\n').replace(
        '    host: root@9.10.11.12\n', '    host: root@9.10.11.12\n    password: "p2"\n')
    (tree / 'config.yaml').write_text(legacy)
    (tree / 'config.yaml').chmod(0o600)
    failed = inproc(FAKE_RULES=json.dumps([['--tags 30-frontend ', 2, 0]]))
    assert failed.returncode and (tree / 'config.yaml').read_text() == legacy
    done = inproc(FAKE_RULES='[]')
    assert done.returncode == 0, done.stderr
    assert (tree / 'config.yaml').read_text() == CONFIG and (tree / 'config.yaml').stat().st_mode & 0o777 == 0o600
    assert 'пароли root entry и экзитов убраны из config.yaml' in done.stderr
    assert inproc().returncode == 0 and (tree / 'config.yaml').read_text() == CONFIG


def test_missing_admin_key_refuses_to_start(tree, inproc):
    (tree / 'config.yaml').write_text(re.sub(r'(?m)^  ssh_pubkey: .*\n', '', CONFIG))
    done = inproc()
    assert done.returncode == 1 and 'admin.ssh_pubkey в config.yaml не задан' in done.stderr
    assert not plays(tree) and not plays(tree, 'ssh') and not inproc.services


def test_admin_key_of_an_installed_entry_may_already_be_on_it(tmp_path, monkeypatch):
    """Обновление с 1.2.0 без admin.ssh_pubkey: хватает ключа человека в authorized_keys entry, свои ключи узла не в счёт."""
    from vpn2 import deploy as orchestrator
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.ssh').mkdir()
    own = 'ssh-ed25519 AAAAown anysda-orchestrator\n'
    (tmp_path / '.ssh/id_ed25519.pub').write_text(own)
    (tmp_path / '.ssh/authorized_keys').write_text(own)
    for installed, keys, env in ((True, own, {}), (False, own + 'ssh-ed25519 AAAAhuman me\n', {}),
                                 (False, '', {'ADMIN_SSH_PUBKEY': 'not a key'})):
        monkeypatch.setattr(orchestrator, 'installed', lambda installed=installed: installed)
        (tmp_path / '.ssh/authorized_keys').write_text(keys)
        with pytest.raises(SystemExit):
            orchestrator.admin_key_check(env)
    (tmp_path / '.ssh/authorized_keys').write_text(own + 'ssh-ed25519 AAAAhuman me\n')
    monkeypatch.setattr(orchestrator, 'installed', lambda: True)
    orchestrator.admin_key_check({})
    monkeypatch.setattr(orchestrator, 'installed', lambda: False)
    orchestrator.admin_key_check({'ADMIN_SSH_PUBKEY': 'ssh-ed25519 AAAAx me'})


def test_recover_skips_the_journal_check(tree, inproc):
    done = inproc('recover', FAKE_OPEN='1')
    assert done.returncode == 0, done.stderr
    assert 'journal=False' in plays(tree)[0]['args']


def test_unfinished_deploy_is_refused_without_changes(tree, inproc):
    done = inproc(FAKE_OPEN='1')
    assert done.returncode == 1 and 'unfinished deploy on ru: operation journal is open' in done.stderr
    assert [call['args'].split('--tags ')[1].split()[0] for call in plays(tree)] == ['release']


def test_failed_release_checks_close_the_journal(tree, inproc):
    done = inproc(FAKE_RULES=json.dumps([['--tags release', 2, 0]]))
    assert done.returncode == 1
    assert [call['args'].split('--tags ')[1].split()[0] for call in plays(tree)] == ['release', 'abort']


def test_failed_stage_stops_the_deploy_and_keeps_the_journal(tree, inproc):
    done = inproc(FAKE_RULES=json.dumps([['--tags 20-ru-router ', 2, 0]]))
    assert done.returncode == 1
    assert 'стадия 20-ru-router: одна или несколько нод завершились с ошибкой' in done.stderr
    assert 'deploy interrupted: operation journal retained' in done.stderr
    assert not [call for call in plays(tree) if '--tags 21-' in call['args'] or '--tags abort' in call['args']]


def test_failed_stage_of_the_group_waits_for_the_others_and_keeps_the_journal(tree, inproc):
    done = inproc(FAKE_RULES=json.dumps([['--tags 22-adguard ', 2, 0], ['--tags 26-backup ', 0, 1]]))
    assert done.returncode == 1
    assert 'стадия 22-adguard: одна или несколько нод завершились с ошибкой' in done.stderr
    assert '[deploy] [26-backup → ru] done' in done.stderr and 'deploy interrupted: operation journal retained' in done.stderr
    assert not [call for call in plays(tree) if '--tags 27-' in call['args'] or '--tags abort' in call['args']]
    # Сторож упавшей стадии снят как у любой стадии, что кончилась сама.
    assert [call for call in plays(tree, 'ssh') if 'disarm 22-adguard' in call['command']]


def test_failed_stage_stops_its_chain_and_the_next_chain_finishes(tree, inproc):
    done = inproc(FAKE_RULES=json.dumps([['--tags 29-control ', 2, 0], ['--tags 27-ikev2 ', 0, 1]]))
    assert done.returncode == 1 and 'стадия 29-control: одна или несколько нод' in done.stderr
    assert '[deploy] [27-ikev2 → ru] done' in done.stderr and 'deploy interrupted: operation journal retained' in done.stderr
    assert not [call for call in plays(tree) if re.search(r'--tags (35|30)-', call['args'])]


def test_failed_exit_track_stops_the_deploy_before_the_stage_that_needs_exits(tree, inproc):
    done = inproc(FAKE_RULES=json.dumps([['--tags 10-foreign ', 2, 1]]))
    assert done.returncode == 1
    assert 'стадия 10-foreign: одна или несколько нод завершились с ошибкой' in done.stderr
    args = [call['args'] for call in plays(tree)]
    assert any('--tags 19-yt-zapret ' in line for line in args), 'стадии entry шли рядом с экзитами'
    assert not [line for line in args if '--tags 20-' in line or '--tags abort' in line]


@pytest.mark.parametrize('sig, code', [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_killed_in_firewall_stage(tree, sig, code):
    """Сигнал посреди стадии с фаерволом: плей стадии и пульс гаснут, журнал узлов остаётся для recover."""
    proc = orchestrate(tree, wait=False, FAKE_RULES=json.dumps([['--tags 05-mgmt-mesh ', 0, 60]]), VPN2_FW_GUARD_WINDOW='30')
    for _ in range(300):
        if any('--tags 05-mgmt-mesh ' in call['args'] for call in plays(tree)):
            break
        time.sleep(0.1)
    # Пульс сторожа этой стадии пошёл повторно: круг по всем трём узлам после arm.
    def touches():
        return len([call for call in plays(tree, 'ssh') if call['command'].startswith('touch')])

    armed, deadline = touches(), time.monotonic() + 30
    while touches() < armed + 3:
        assert time.monotonic() < deadline, 'пульс сторожа не пошёл повторно'
        time.sleep(0.1)
    proc.send_signal(sig)
    _out, err = proc.communicate(timeout=30)
    assert proc.returncode == code, err
    assert 'deploy interrupted: operation journal retained' in err
    staged = next(call for call in plays(tree) if '--tags 05-mgmt-mesh ' in call['args'])
    assert not alive(staged['pid'])
    beats = [call['host'] for call in plays(tree, 'ssh') if call['command'].startswith('touch')]
    assert {'1.2.3.4', '5.6.7.8', '9.10.11.12'} <= set(beats) and len(beats) > 3, 'пульс шёл повторно на все узлы'
    assert not [call for call in plays(tree, 'ssh') if 'disarm 05-mgmt-mesh' in call['command']]
    assert not [call for call in plays(tree) if '--tags abort' in call['args']]


@pytest.mark.parametrize('ports, touched', [([0, 0], 1), ([0, 1], 1), ([1, 1], 0)])
def test_entry_pulse_waits_for_exits_once_they_have_seen_entry(ports, touched):
    """Сторож entry слышит пульс, только пока порт entry виден снаружи; без экзитов, видевших его, - всегда."""
    from vpn2.deploy import run
    deploy = run.Deploy.__new__(run.Deploy)
    deploy.logins, deploy.outside, calls = {'ru': ['1.2.3.4']}, True, []
    seen = iter(ports)
    deploy.fresh = lambda node, _command, **_options: calls.append(node) or (next(seen) if node != 'ru' else 0)
    deploy.pulse(['ru', 'us', 'gb'])
    assert calls.count('ru') == touched
    deploy.outside, calls[:] = False, []
    deploy.fresh = lambda node, _command, **_options: calls.append(node) or (1 if node != 'ru' else 0)
    deploy.pulse(['ru', 'us', 'gb'])
    assert calls.count('ru') == 1 and not deploy.outside


@pytest.mark.parametrize('codes, failed, tries', [
    ({'us': [255, 0]}, {}, {'ru': 1, 'us': 2}),
    ({'us': [1]}, {'us': 1}, {'ru': 1, 'us': 1}),
    ({'us': [255] * 3}, {'us': 255}, {'ru': 1, 'us': 3}),
])
def test_guard_arm_retries_only_failed_logins(monkeypatch, codes, failed, tries):
    """Сорванный вход (255) повторяется только на своём узле; сторож, что упал сам, не повторяется."""
    from vpn2.deploy import run
    monkeypatch.setattr(run, 'ARM_PAUSE', 0)
    deploy, calls = run.Deploy.__new__(run.Deploy), []
    answers = {node: iter(found) for node, found in codes.items()}
    deploy.fresh = lambda node, _command, **_options: calls.append(node) or next(answers.get(node, iter([0])))
    assert deploy.guard_arm('10-foreign', 'ru,us') == failed
    assert {node: calls.count(node) for node in tries} == tries
