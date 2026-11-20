"""Стадии деплоя на узлах: ansible-playbook из venv, сторож фаервола, 99-verify, выпуск."""
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vpn2 import fsutil, ports, release, stages
from vpn2.config import STATE_DIR
from vpn2.deploy import (CONFIG, INFRA, REPO, ROOT, G, R, Y, E, die, elapsed, installed, log, ok, orchestrator_key, prep_keys,
                         key_id, own_keys, read, report, ssh_options, step, warn)
from vpn2.deploy.plays import Plays

PLAYBOOK, SITE = INFRA / 'ansible/deploy.yml', INFRA / 'ansible/site.yml'
# Стадии, что трогают фаервол: на время каждой узел держит сторожа (vpn2/fwguard.py).
GUARDED = {'00-bootstrap', '05-mgmt-mesh', '10-foreign', '19-yt-zapret', '20-ru-router', '22-adguard',
           '23-emergency', '27-ikev2', '28-wireguard', '29-openvpn', '30-frontend'}
GUARD_WINDOW = int(os.environ.get('VPN2_FW_GUARD_WINDOW') or 90)
# Шесть пульсов на окно сторожа хватает и на пути с потерями.
BEAT_EVERY = max(5, GUARD_WINDOW // 6)
# Попытки поставить сторожа, когда новый вход на узел не прошёл, и пауза между ними, с.
ARM_TRIES, ARM_PAUSE = 3, 5
GUARD = '/run/vpn2-fw-guard'
QUIET = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
# Экзитная часть стадии идёт рядом со следующими стадиями entry, которым экзиты не нужны, до названной:
# секреты экзитов забирает 20-ru-router, аварийка экзитов до панели не нужна никому.
BESIDE = {'10-foreign': '20-ru-router', '23-emergency': '30-frontend'}
# WARP экзита в полном деплое поднимается своим юнитом, итог - в итоговом 99-verify: до 20-ru-router его не ждут.
WARP_LATER = ('-e', '{"exit_warp_wait": false}')
# Группы полного деплоя: цепочки стадий entry идут разом, стадии цепочки - по порядку site.yml. Общих писателей
# на узле у цепочек группы нет: пользователи и группы, ufw, цепочка nft, compose.yaml, apt с пользователями пакетов,
# сторож фаервола (он один на узел) - у одной цепочки группы.
TOGETHER = (
    (('21-failover-watchdog',), ('22-adguard',), ('25-monitoring',), ('26-backup',)),
    (('27-ikev2',), ('29-control', '35-telegram')),
)
# Окружение стадии в полном деплое: панель ставится следом и сама берёт новый секрет бота.
FULL_ENV = {'35-telegram': {'VPN2_PANEL_FOLLOWS': 'true'}}
# Смена состава экзитов: стадии нового экзита, затем стадии entry, что читают список экзитов (маршруты,
# наблюдатели аварийки на всех узлах, цели мониторинга, панель) и проверка всех узлов.
EXIT_FIRST = ('00-bootstrap', '05-mgmt-mesh', '10-foreign')
EXIT_STAGES = ('20-ru-router', '23-emergency', '25-monitoring', '30-frontend', '99-verify')


class Interrupted(SystemExit):
    """Сигнал оператора или systemd: код 130/143, перекатка портов его не глотает."""


class Deploy:
    def __init__(self, development, manifest):
        self.development, self.manifest = development, manifest
        self.tmp = Path(tempfile.mkdtemp())
        self.stages = stages.load(SITE)
        self.stage_log, self.verify_warnings, self.deploy_warnings, self.skipped = [], [], [], []
        self.mutating = self.opened = self.staged = self.guard_warned = self.closing = self.old_layout = False
        self.plays, self.beat, self.nodes, self.logins = None, None, {}, {}
        self.outside = self.pulled = False
        # Ход задания менеджера установки (vpn2.manager.Progress), если деплой идёт заданием.
        self.progress = self.result = None
        self.lock = threading.Lock()
        self.load_nodes()

    def load_nodes(self):
        """Переменные узлов из config.yaml - те же, что видит ansible через inventory.py."""
        listed = subprocess.run([sys.executable, '-B', INFRA / 'ansible/inventory.py', '--list'], capture_output=True, text=True,
                                env={**os.environ, 'VPN2_CONFIG': str(CONFIG), 'VPN2_INVENTORY_SECRETS': 'show'})
        if listed.returncode:
            sys.stderr.write(listed.stderr)
            raise SystemExit(1)
        hosts = json.loads(listed.stdout)['_meta']['hostvars']
        plain = lambda value: value['__ansible_unsafe'] if isinstance(value, dict) else value  # noqa: E731
        self.nodes = {name: {key: plain(value) for key, value in found.get('vpn2_env', {}).items()} for name, found in hosts.items()}
        self.logins = {name: [plain(found[key]) for key in ('ansible_host', 'ansible_ssh_common_args')]
                       for name, found in hosts.items()}

    def env(self, node='ru'):
        return self.nodes.get(node, {})

    def exit_tags(self):
        return self.env().get('EXIT_TAGS', '').split()

    def expand(self, group):
        """Группа -> узлы; экзиты, пропущенные в pre-flight, в стадии не идут."""
        tags = self.exit_tags()
        active = [tag for tag in tags if tag not in self.skipped]
        if group == 'ru':
            return ['ru']
        if group in ('foreign', 'all'):
            if not tags:
                die('EXIT_TAGS пустой — перезапусти ./deploy.sh')
            return active if group == 'foreign' else ['ru', *active]
        if group not in active:
            die(f'unknown host group: {group} (доступные: ru foreign all или тег из config.yaml)')
        return [group]

    def stage_nodes(self, stage, hosts, refuse_empty=False):
        try:
            return ','.join(stages.nodes(self.stages, stage, hosts, refuse_empty=refuse_empty))
        except stages.StageError as error:
            die(str(error))

    def play(self, *args, stdout=None, stderr=None, env=None):
        """ansible-playbook с этими аргументами; все плеи запуска - из одного прогретого процесса (plays.py)."""
        # Деплой закрывается: потоки цепочек и пульса новых плеев не начинают.
        if self.closing and threading.current_thread() is not threading.main_thread():
            raise SystemExit(1)
        env = {**os.environ, 'ANSIBLE_CONFIG': str(INFRA / 'ansible/ansible.cfg'), 'VPN2_CONFIG': str(CONFIG), **(env or {})}
        with self.lock:
            self.plays = self.plays or Plays(ROOT, env)
        return self.plays.run(list(map(str, args)), env, stdout, stderr)

    def service(self, *args, **options):
        return self.play(PLAYBOOK, '-e', f'manifest={self.manifest}', *args, **options)

    def ssh(self, node, command):
        """ssh на узел с тем же ключом, что у ansible, без общего соединения: открытое соединение живёт
        и при сломанном правиле."""
        host, options = self.logins[node]
        return ['ssh', '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'BatchMode=yes', *shlex.split(options),
                '-i', str(orchestrator_key()), f'root@{host}', command]

    def fresh(self, node, command, show=False):
        """Новый ssh-вход на узел (ssh()). Код команды; 255 - вход не прошёл. Вывод show печатает деплой:
        ssh без tty ставит O_NONBLOCK на свой stdout, а общий нужен ansible-playbook."""
        try:
            done = subprocess.run(self.ssh(node, command), stdin=subprocess.DEVNULL, text=True,
                                  stdout=subprocess.PIPE if show else subprocess.DEVNULL, stderr=subprocess.PIPE if show else subprocess.DEVNULL,
                                  timeout=30)
        except subprocess.TimeoutExpired:
            return 255
        print(done.stdout or '', done.stderr or '', sep='', end='', flush=True)
        return done.returncode

    def each(self, nodes, command, **options):
        if not nodes:
            return {}
        with ThreadPoolExecutor(len(nodes)) as pool:
            return dict(zip(nodes, pool.map(lambda node: self.fresh(node, command(node), **options), nodes)))

    def pulse(self, nodes):
        """Пульс: экзиты заодно проверяют порт ssh entry. Когда его видит хоть один экзит, пульс на
        entry идёт, только пока видит: сам entry до себя дойдёт и при сломанном правиле."""
        entry = self.logins['ru'][0]
        seen = self.each([node for node in nodes if node != 'ru'],
                         lambda _node: f"touch -c {GUARD}/alive 2>/dev/null; timeout 3 bash -c '</dev/tcp/{entry}/22'")
        self.outside |= 0 in seen.values()
        if 'ru' in nodes and (not self.outside or 0 in seen.values()):
            self.fresh('ru', f'touch -c {GUARD}/alive')

    def guard_beat(self):
        """Пульс сторожу раз в BEAT_EVERY с от первой стадии со сторожем до конца деплоя; первый даёт arm.
        Между стадиями пульс идёт вхолостую (touch -c без сторожа), зато стадия не ждёт своего первого."""
        with self.lock:
            if self.beat:
                return
            nodes = self.expand('all')
            self.pulse(nodes)
            if not self.outside and not self.guard_warned:
                self.guard_warned = True
                warn('[fw-guard] вход на entry снаружи не проверяется: до стадии его не видит ни один экзит')
            stop = threading.Event()

            def beat():
                while not stop.wait(BEAT_EVERY) and not self.closing:
                    self.pulse(nodes)

            self.beat = (stop, threading.Thread(target=beat, daemon=True))
            self.beat[1].start()

    def stop_beat(self):
        with self.lock:
            if not self.beat:
                return
            stop, thread = self.beat
            self.beat = None
        stop.set()
        thread.join()

    def guard_arm(self, stage, nodes):
        """Сторож на узлах стадии; коды узлов, где не встал. Вход, что не прошёл (255), повторяется:
        стадия ещё ничего не меняла, а новый ssh-вход изредка срывается и на исправной сети."""
        left = nodes.split(',')
        for attempt in range(ARM_TRIES):
            armed = self.each(left, lambda _node: f'python3 -I /tmp/anysda/fwguard.py arm {stage} {GUARD_WINDOW} 2>&1', show=True)
            failed = {node: code for node, code in armed.items() if code}
            if not failed or set(failed.values()) != {255} or attempt == ARM_TRIES - 1:
                return failed
            left = list(failed)
            warn(f'[{stage} → {",".join(left)}] вход для сторожа не прошёл, повторяю')
            time.sleep(ARM_PAUSE)

    def guard_finish(self, stage, nodes):
        """Сторож снят новым входом; узел, что не пускает, ждёт окно сторожа: он вернёт фаервол, и вход появится."""
        deadline, left, code = time.time() + GUARD_WINDOW + 30, nodes.split(','), 0
        while True:
            done = self.each(left, lambda _node: f'python3 -I {GUARD}/fwguard.py disarm {stage}', show=True)
            code = code or any(found not in (0, 255) for found in done.values())
            left = [node for node, found in done.items() if found == 255]
            if not left or time.time() >= deadline:
                break
            time.sleep(5)
        if left:
            warn(f'после стадии {stage} нода не пускает новые ssh-входы дольше окна сторожа - проверь вход руками')
        return 1 if code or left else 0

    def bundle(self):
        """Пакет vpn2 для /tmp/anysda узлов, один на запуск."""
        path = self.tmp / 'vpn2.tgz'
        if not path.exists() and subprocess.run(['tar', '-C', INFRA / 'vpn2/src', '--exclude=__pycache__', '-czf', path, 'vpn2']).returncode:
            raise SystemExit(1)
        return f'bundle={path}'

    def stage_once(self, stage, nodes, env=None, extra=(), held=False):
        """Одна стадия на узлах: сторож, плей стадии, снятие сторожа. /tmp/anysda выложил выпуск,
        без выпуска (99-verify) - тот же плей тегом prepare. held - вывод плея одним блоком по его концу."""
        log(f'[{stage} → {nodes}] starting')
        self.staged = True
        tags = stage if self.opened else f'prepare,{stage}'
        guard = stage in GUARDED
        if guard:
            self.guard_beat()
            if failed := self.guard_arm(stage, nodes):
                warn(f'[{stage} → {nodes}] сторож фаервола не встал ({failed}), стадию не запускаю')
                return self.guard_finish(stage, nodes) or 1
        if held:
            # Плеи деплоя делят контекст Mitogen узла, а он исполняет модули по одному: у плеев,
            # что идут разом, каждый модуль - в своём fork на узле.
            extra = (*extra, '-e', 'mitogen_task_isolation=fork')
            if sys.stdout.isatty():
                env = {**(env or {}), 'ANSIBLE_FORCE_COLOR': '1'}
        with tempfile.TemporaryFile('w+') if held else contextlib.nullcontext() as out:
            code = self.play(PLAYBOOK, SITE, '--tags', tags, '--limit', nodes, '-e', self.bundle(), '-e', f'manifest={self.manifest}',
                             *extra, stdout=out, stderr=out, env=env)
            if out:
                out.seek(0)
                with self.lock:
                    sys.stdout.write(out.read())
                    sys.stdout.flush()
        # Прерванный деплой сторожа не снимает: без пульса он вернёт фаервол сам.
        if self.closing:
            raise SystemExit(1)
        if guard:
            code = self.guard_finish(stage, nodes) and 1 or code
        return code

    def verify(self, nodes):
        """99-verify: 0 - всё измерено, 2 - не всё (установка годна), 1 - отказ."""
        out = Path(tempfile.mkdtemp(dir=self.tmp))
        layout = ('-e', 'verify_old_layout=true') if self.old_layout else ()
        code, result = self.stage_once('99-verify', nodes, env={'VPN2_VERIFY_OUT': str(out)}, extra=layout), 0
        for host in nodes.split(','):
            found = (out / host).read_text().split('\n', 1) if (out / host).is_file() else ['']
            text = found[1].rstrip('\n') if len(found) > 1 else ''
            if text:
                print(text, flush=True)
            self.deploy_warnings += re.findall(r'(?m)^\[.*\] ⚠ WARP: .*$', text)
            if code == 0 and found[0] == '2':
                warn(f'[99-verify → {host}] не все проверки измерены')
                self.verify_warnings += re.findall(r'(?m)^\[.*\] \? .*не проверено:.*$', text) \
                    or [f'[{host}] ? не проверено: 99-verify вернул код 2; подробности выше']
                result = result or 2
            elif code or found[0] != '0':
                result = 1
            else:
                ok(f'[99-verify → {host}] done')
        return result

    def run_stage(self, stage, group, extra=(), held=False, env=None):
        """Стадия на группе; задание менеджера установки (VPN2_JOB) видит её ход."""
        if not self.progress:
            return self.stage_on(stage, group, extra, held, env)
        nodes = self.stage_nodes(stage, self.expand(group))
        with self.progress.stage(stage, nodes.split(',') if nodes else []):
            return self.stage_on(stage, group, extra, held, env)

    def stage_on(self, stage, group, extra, held, env):
        t0, code = time.time(), 0
        nodes = self.stage_nodes(stage, self.expand(group))
        if not nodes:
            return 0
        if stage == '00-bootstrap':
            prep_keys()
        elif stage == '20-ru-router':
            if not self.pulled:
                self.pull_secrets()
            self.warn_insecure_exits()
        elif stage == '23-emergency' and subprocess.run([sys.executable, '-B', '-m', 'vpn2.emergency', 'provision', REPO]).returncode:
            raise SystemExit(1)
        if stage == '10-foreign':
            if len(self.exit_tags()) == 1:
                warn('единственный экзит: на время его обновления возможен перерыв VPN')
            # Экзиты обновляются по одному, следующий - только когда предыдущий прошёл проверку.
            for host in nodes.split(','):
                if self.stage_once(stage, host, extra=extra) or self.verify(host):
                    code = 1
                    break
        elif stage == '99-verify':
            for host in self.skipped:
                print(f'[{host}] ⊘ пропущен: SSH недоступен в pre-flight', flush=True)
                code = 2
            code = {0: code, 2: 2}.get(self.verify(nodes), 1)
            self.warn_insecure_exits()
        elif self.stage_once(stage, nodes, env=env, held=held):
            code = 1
        else:
            ok(f'[{stage} → {nodes}] done')
        if code == 1:
            die(f'стадия {stage}: одна или несколько нод завершились с ошибкой')
        self.stage_log.append(f'{stage}\t{group}\t{elapsed(t0)}')
        if stage == '99-verify' and self.verify_warnings:
            warn('99-verify: ⚠ не все проверки измерены; установка не требует отката')
        return code

    def rerun(self, path, pairs):
        """Перекатка стадий для vpn2.ports с выводом в журнал; итог стадий в сводку не идёт."""
        kept = list(self.stage_log), list(self.verify_warnings), list(self.deploy_warnings)
        self.load_nodes()
        sys.stdout.flush()
        sys.stderr.flush()
        saved = os.dup(1), os.dup(2)
        try:
            with open(path, 'w', buffering=1) as journal, contextlib.redirect_stdout(journal), contextlib.redirect_stderr(journal):
                os.dup2(journal.fileno(), 1)
                os.dup2(journal.fileno(), 2)
                for stage, group in pairs:
                    self.run_stage(stage, group)
            return True
        except Interrupted:
            raise
        except SystemExit:
            return False
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])
            self.stage_log, self.verify_warnings, self.deploy_warnings = kept

    def rotate_ports(self):
        """После 20-ru-router: канал hy2-direct экзитов, подбор порта и исключение мёртвых."""
        step('verify: проверяю tunnels via clash-api + UDP-пробник для мертвецов')
        rows = ports.check(self.expand('foreign'), self.rerun)
        self.stage_log += rows

    def beside(self, stage):
        """Экзитная часть стадии своим потоком; join() дожидается её и валит деплой, если она упала.
        Секреты экзитов для 20-ru-router тянет та же цепочка, пока entry ещё в своих стадиях."""
        failed = []

        def track():
            try:
                self.run_stage(stage, 'foreign', WARP_LATER if stage == '10-foreign' else ())
                if stage == '10-foreign':
                    self.pull_secrets()
            except BaseException as error:
                failed.append(error)

        thread = threading.Thread(target=track, daemon=True)
        thread.start()
        return thread, failed, BESIDE[stage]

    def together(self, chains):
        """Цепочки стадий entry разом, вывод каждой стадии - блоком. Упавшая стадия останавливает свою цепочку
        и валит деплой, когда закончат остальные. Строки сводки - в порядке site.yml."""
        failed, rows, names = [], len(self.stage_log), [name for chain in chains for name in chain]

        def track(chain):
            try:
                for name in chain:
                    self.run_stage(name, 'ru', held=True, env=FULL_ENV.get(name))
            except BaseException as error:
                failed.append(error)

        threads = [threading.Thread(target=track, args=(chain,), daemon=True) for chain in chains]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if failed:
            raise failed[0]
        # Строки цепочки экзитов, что пришли за это время, - следом за группой.
        order = {name: index for index, name in enumerate(names)}
        self.stage_log[rows:] = sorted(self.stage_log[rows:], key=lambda row: order.get(row.split('\t')[0], len(names)))

    @staticmethod
    def join(side):
        side[0].join()
        if side[1]:
            raise side[1][0]

    def do_all(self):
        t0, verify, side = time.time(), 0, None
        try:
            for stage in self.stages:
                if side and stage.name == side[2]:
                    self.join(side)
                    side = None
                if chains := next((found for found in TOGETHER if stage.name in sum(found, ())), None):
                    if stage.name == chains[0][0]:
                        self.together(chains)
                    continue
                group = {'entry': 'ru', 'exits': 'foreign'}.get(stage.hosts, 'all')
                if stage.name in BESIDE:
                    side = self.beside(stage.name)
                    if group == 'foreign':
                        continue
                    group = 'ru'
                if stage.name == '99-verify':
                    # Остатки раскладки v1.1.5 убирает тот же плей за зелёной проверкой узла.
                    self.old_layout = True
                    verify = self.run_stage(stage.name, group)
                else:
                    self.run_stage(stage.name, group, env=FULL_ENV.get(stage.name))
                if stage.name == '20-ru-router':
                    self.rotate_ports()
        except BaseException as error:
            # Брошенная на полпути цепочка экзитов хуже ожидания; сигнал оператора не ждёт.
            if side and not isinstance(error, Interrupted):
                side[0].join()
            raise
        self.release_complete()
        report.summary(self, t0)
        report.announce(self)
        return verify

    def add_exit(self, tag):
        """Новый экзит в config.yaml: его стадии, затем стадии entry, что читают состав экзитов."""
        if tag in self.skipped:
            die(f'{tag}: нет входа по ssh, экзит не добавлен')
        for stage in EXIT_FIRST:
            self.run_stage(stage, tag)
        return self.exits_changed(tag)

    def remove_exit(self, tag, host):
        """Экзит уже убран из config.yaml: entry перестраивается без него, затем убираются его следы."""
        code = self.exits_changed()
        with self.progress.stage('cleanup', [tag]) if self.progress else contextlib.nullcontext():
            self.result = forget_exit(self, tag, host)
        return code

    def exits_changed(self, new=None):
        code = 0
        for stage in EXIT_STAGES:
            code = self.run_stage(stage, 'all' if stage in ('23-emergency', '99-verify') else 'ru') or code
            if stage == '20-ru-router' and new:
                self.stage_log += ports.check([new], self.rerun)
        self.release_complete()
        return code

    def release_cli(self, *args):
        if release.main(['--manifest', self.manifest, *args, str(REPO)]):
            raise SystemExit(1)

    def release_start(self, action, scope=''):
        """Выпуск открывается до изменений одним плеем: журнал прошлого деплоя закрыт, /tmp/anysda и apt
        готовы, на каждом узле проверки и снимок. С фазы applying узлы считаются изменёнными."""
        saved = STATE_DIR / 'release-manifest.json'
        if self.development or action == 'rollback':
            self.manifest = ''
        else:
            # Без --manifest берётся опубликованный манифест commit этого дерева.
            if not self.manifest:
                self.manifest = str(saved)
                self.release_cli('fetch')
            self.release_cli('verify-tree')
            if not (saved.exists() and os.path.samefile(self.manifest, saved)):
                shutil.copyfile(self.manifest, saved)
                saved.chmod(0o600)
        if action == 'rollback':
            if code := self.service('--tags', 'artifacts', '--limit', 'ru', '-e', self.bundle(), stdout=subprocess.DEVNULL):
                raise SystemExit(code)
            return
        self.opened = self.staged = True
        if self.service('--tags', 'release', '-e', self.bundle(), '-e', f'out={self.tmp}', '-e', f'scope={scope}',
                        '-e', f'partial={action not in ("", "recover")}', '-e', f'journal={action != "recover"}',
                        '-e', f'bootstrap={action in ("", "recover", "00-bootstrap", "exit-add")}'):
            if found := sorted(self.tmp.glob('*.journal')):
                self.opened = False
                die(found[0].read_text().strip())
            raise SystemExit(1)
        self.mutating = True

    def release_complete(self):
        """Запись состава и закрытие журнала на каждом узле; пакет ещё лежит в /tmp/anysda."""
        if not self.mutating:
            return
        state = 'complete --role {}' if self.manifest else 'phase --phase complete'
        left = ['ru', *self.exit_tags()]
        for _ in range(3):
            done = self.each(left, lambda node: 'PYTHONPATH=/tmp/anysda python3 -m vpn2.cli state '
                             + state.format('entry' if node == 'ru' else 'exit'))
            if not (left := [node for node, code in done.items() if code]):
                break
        if left:
            die('запись о выпуске не закрыта: повтори ./deploy.sh recover')
        self.mutating = False

    def close(self, code):
        """Выход деплоя: кто бы ни прервал, дочерние плеи гаснут, журнал узлов честен."""
        self.closing = True
        self.stop_beat()
        if self.plays:
            self.plays.stop()
        if code and self.mutating:
            print('deploy interrupted: operation journal retained; inspect services, then run ./deploy.sh recover '
                  '(or ./deploy.sh rollback)', file=sys.stderr)
        elif code and self.opened:
            self.service('--tags', 'abort', **QUIET)
        # Пакет и манифест на экзитах живут до конца запуска.
        if self.staged:
            self.each([tag for tag in self.exit_tags() if tag not in self.skipped], lambda _node: 'rm -rf /tmp/anysda')
        if self.plays:
            self.plays.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def warn_insecure_exits(self):
        for host in self.exit_tags():
            if self.env().get(f'{host.upper().replace("-", "_")}_HY2_INSECURE') == '1':
                warn(f'{host}: exits[].hy2_insecure: true — сертификат экзита не проверяется, '
                     'подмену экзита на пути entry → экзит не видно')

    def pull_secrets(self):
        """Перед 20-ru-router: пароли каналов, сертификат Hysteria2, host-key и ключ Reality каждого экзита."""
        log('prep: тяну секреты Hysteria2 + clash-api с экзит-нод')
        self.pulled = True
        folder = INFRA / 'secrets'
        secrets = folder / 'foreign-secrets.env'
        cached = secrets.read_text() if secrets.is_file() else ''
        for sub in (folder, folder / 'exit-certs', folder / 'exit-hostkeys'):
            sub.mkdir(parents=True, exist_ok=True)
            sub.chmod(0o700)
        # Файл секретов закрыт с первого байта: сбой посередине оставляет его 0600.
        os.close(os.open(secrets, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        secrets.chmod(0o600)
        active, got = self.expand('foreign'), Path(tempfile.mkdtemp(dir=self.tmp))
        if active:
            with open(got / 'log', 'w') as out:
                failed = self.service('--tags', 'secrets', '--limit', ','.join(active), '-e', f'out={got}', stdout=out, stderr=out)
            if failed:
                lines = [line for line in (got / 'log').read_text().splitlines()
                         if line and not re.match(r'(PLAY|TASK|ok:|skipping:)', line)]
                print('\n'.join(lines[-8:]), file=sys.stderr)
                die('секреты экзитов не получены, стадию 20 не запускаю')
        text = ''
        for host in self.exit_tags():
            if host not in active:
                upper = host.upper().replace('-', '_')
                kept = re.findall(rf'(?m)^{upper}_(?:DIRECT|WARP|OBFS|MGMT|CLASH)=.*$', cached)
                if len(kept) != 5:
                    die(f'{host} пропущен в pre-flight, но в прошлом деплое нет его 5 секретов; сначала верни SSH и запусти ./deploy.sh')
                text += ''.join(f'{line}\n' for line in kept)
                warn(f'prep: {host} пропущен, использую сохранённые секреты')
                continue
            text += (got / f'{host}.env').read_text()
            pem, pub, reality = (got / f'{host}.{kind}' for kind in ('pem', 'pub', 'reality'))
            if pem.read_text().strip():
                if subprocess.run(['openssl', 'x509', '-noout', '-in', pem], capture_output=True).returncode:
                    die(f'{host} отдал не PEM-сертификат Hysteria2')
                install(pem, folder / f'exit-certs/{host}.pem', 0o600)
            else:
                warn(f'на {host} нет сертификата Hysteria2 — закрепится ранее сохранённый на entry, '
                     'иначе 20-ru-router откажет (передеплой 10-foreign)')
            if pub.read_text().strip():
                if subprocess.run(['ssh-keygen', '-lf', pub], capture_output=True).returncode:
                    die(f'{host} отдал не ssh-ключ в /etc/ssh/ssh_host_ed25519_key.pub')
                install(pub, folder / f'exit-hostkeys/{host}.pub', 0o644)
            lines = reality.read_text().splitlines()
            if len(lines) == 4 and all(re.fullmatch(r'PUB=[A-Za-z0-9_-]{43}|SID=[0-9a-f]{0,16}|SNI=[A-Za-z0-9.-]{1,253}|PORT=[0-9]{1,5}', line)
                                       for line in lines):
                install(reality, folder / f'exit-certs/{host}.reality', 0o600)
            else:
                warn(f'на {host} нет ключа Reality - запасной TCP-канал до него не включится (передеплой 10-foreign)')
        secrets.write_text(text)
        ok('секреты записаны в secrets/foreign-secrets.env')

    def knock(self, node, deadline):
        """Вход по ключу на узел; '' - вошёл, иначе текст отказа ssh. Без связи вход повторяется до deadline:
        узел после переустановки ОС поднимает sshd не сразу."""
        while True:
            try:
                done = subprocess.run(self.ssh(node, 'true'), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                said = '\n'.join(line for line in done.stderr.splitlines() if not line.startswith('Warning: Permanently added'))
                reason = '' if done.returncode == 0 else said.strip() or f'ssh: код {done.returncode}'
            except subprocess.TimeoutExpired:
                reason = 'нет ответа за 30 с'
            if not reason or refused(reason) or time.time() >= deadline:
                return reason
            time.sleep(5)

    def preflight(self):
        """Вход по ключу на все узлы разом, до любых изменений на них и на entry. Отказ во входе и сменившийся
        host-key останавливают деплой одним списком узлов. Экзит без связи пропускается только при обновлении:
        у него есть секреты прошлого деплоя; в чистой установке это тоже отказ."""
        step('pre-flight: вход по ключу на узлы')
        nodes = self.expand('all')
        with self.progress.stage('preflight', nodes) if self.progress else contextlib.nullcontext({}) as record:
            self.logins_ok(nodes, record)

    def logins_ok(self, nodes, record):
        deadline = time.time() + int(os.environ.get('PREFLIGHT_BUDGET') or 90)
        with ThreadPoolExecutor(len(nodes)) as pool:
            found = dict(zip(nodes, pool.map(lambda node: self.knock(node, deadline), nodes)))
        record['failed'] = [host for host, reason in found.items() if reason]
        reasons, fixes, auth, net = [], [], False, False
        for host, reason in found.items():
            if not reason:
                print(f'  {host:<30} {G}✓{E}', flush=True)
                continue
            addr, reason = self.logins[host][0], reason.replace('\n', ' ')
            if 'IDENTIFICATION HAS CHANGED' in reason or 'Host key verification failed' in reason:
                fixes.append(f'ssh-keygen -R {addr} -f {STATE_DIR}/known_hosts')
            elif refused(reason):
                auth = True
            elif host != 'ru' and installed():
                self.skipped.append(host)
                print(f'  {Y}⚠ Экзит {host} пропущен: SSH недоступен в pre-flight{E}', flush=True)
                continue
            else:
                net = True
            print(f'  {host:<30} {R}✗{E}', flush=True)
            reasons.append(f'нода {host} ({addr}) не пускает по ключу с entry: {reason}')
        if not reasons:
            if self.skipped:
                print(f'  {Y}⚠ Продолжаю без экзитов: {" ".join(self.skipped)}{E}', flush=True)
            print(flush=True)
            return
        lines = [*reasons]
        if fixes:
            lines += ['Сменился host-key ноды: так бывает после переустановки ОС на том же IP.',
                      'Новый ключ сам не принимается - это защита от подмены ноды.',
                      'Если ОС переустанавливал ты, убери старый ключ и повтори ./deploy.sh:', *(f'  {fix}' for fix in fixes)]
        if auth:
            lines += ['Нода отказала во входе по ключу. Проверь на ноде:',
                      f'  1. в /root/.ssh/authorized_keys есть строка из ./deploy.sh -key: {key_text()}',
                      '  2. ssh на ноде слушает порт 22 и пускает root по ключу (PermitRootLogin не no)']
        if net:
            lines += ['Проверь:', '  1. адрес ноды в config.yaml', '  2. sshd на ноде запущен и слушает порт 22',
                      '  3. firewall / geo-блок на стороне провайдера ноды']
        print(f'\n{R}✗ Не пускают по ключу:{E}\n' + ''.join(f'  {line}\n' for line in lines), end='', flush=True)
        die(f'pre-flight: узлов без входа по ключу: {len(reasons)}, на серверах ничего не менялось')


def forget_exit(deploy, tag, host):
    """Убранный экзит: если сервер отвечает - наши службы, файлы и ключи на нём (lib/exit-forget.sh), затем его
    следы на entry. Итог: server cleaned, partial (left - что осталось на сервере) или forgotten (не ответил)."""
    command = ['ssh', '-o', 'BatchMode=yes', *ssh_options(), '-i', str(orchestrator_key()), f'root@{host}', 'bash -s']
    try:
        keys = '\n'.join(filter(None, (key_id(text) for text in own_keys())))
        done = subprocess.run(command, input=f'ENTRY_KEYS={shlex.quote(keys)}\n' + (INFRA / 'lib/exit-forget.sh').read_text(),
                              capture_output=True,
                              text=True, timeout=600)
        out, code = done.stdout, done.returncode
    except subprocess.TimeoutExpired as error:
        out, code = error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout or '', 255
    print(out, end='', flush=True)
    left = [line.removeprefix('left: ') for line in out.splitlines() if line.startswith('left: ')]
    if 'vpn2-forget: start' not in out:
        server = 'forgotten'
        warn(f'{tag} ({host}) не ответил по ssh: экзит забыт, на сервере ничего не менялось')
    else:
        if 'vpn2-forget: done' not in out:
            left.append('очистка оборвалась на середине')
        server = 'partial' if left or code else 'cleaned'
    secrets = INFRA / 'secrets'
    for path in (secrets / f'exit-certs/{tag}.pem', secrets / f'exit-certs/{tag}.reality',
                 secrets / f'exit-hostkeys/{tag}.pub', secrets / f'emergency/{tag}.json'):
        path.unlink(missing_ok=True)
    for path in (STATE_DIR / 'mgmt-ips.txt', STATE_DIR / 'excluded-exits.txt'):
        if path.is_file():
            path.write_text(''.join(line for line in path.read_text().splitlines(True) if line.split()[:1] != [tag]))
    for path, drop in ((Path('/etc/anysda/exits-disabled.json'), lambda data: {'disabled': [
            item for item in data.get('disabled', []) if item != tag]}),
                       (Path('/etc/anysda/client-prefs.json'), lambda data: {
                           name: ips for name, ips in data.items() if name != tag})):
        if path.is_file():
            fsutil.atomic(path, json.dumps(drop(json.loads(path.read_text())), sort_keys=True) + '\n', 0o644)
    subprocess.run(['ssh-keygen', '-R', host, '-f', STATE_DIR / 'known_hosts'], capture_output=True)
    deploy.deploy_warnings += [f'{tag}: на сервере осталось: {item}' for item in left]
    return {'exit': tag, 'server': server, 'left': left}


def refused(reason):
    """Узел ответил и не пустил: ключа нет или вход root закрыт."""
    return 'Permission denied' in reason or 'Authentication failed' in reason


def key_text():
    return read(f'{orchestrator_key()}.pub') or '(ключа ещё нет)'


def install(src, dst, mode):
    shutil.copyfile(src, dst)
    dst.chmod(mode)
