"""Стадии деплоя на узлах: ansible-playbook из venv, сторож фаервола, 99-verify, выпуск."""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from vpn2 import ports, release, stages
from vpn2.config import STATE_DIR
from vpn2.deploy import CONFIG, INFRA, REPO, ROOT, G, R, Y, E, die, elapsed, log, ok, prep_keys, report, step, warn

PLAYBOOK, SITE = INFRA / 'ansible/deploy.yml', INFRA / 'ansible/site.yml'
# Стадии, что трогают фаервол: на время каждой узел держит сторожа (vpn2/fwguard.py).
GUARDED = {'00-bootstrap', '05-mgmt-mesh', '10-foreign', '19-yt-zapret', '20-ru-router', '22-adguard',
           '23-emergency', '27-ikev2', '28-wireguard', '29-openvpn', '30-frontend'}
GUARD_WINDOW = int(os.environ.get('VPN2_FW_GUARD_WINDOW') or 90)
# Плеи идут на Mitogen (ansible.cfg); pre-flight - ssh-плагином: он разбирает текст отказа ssh.
SSH_PLUGIN = {'ANSIBLE_STRATEGY': 'linear', 'ANSIBLE_TIMEOUT': '10'}
# Пульс сторожу - только новым ssh-входом: открытое соединение живёт и при сломанном правиле.
# Вход без повторов: пульс к пропавшему узлу не задерживает пульс остальным.
FRESH_LOGIN = {**SSH_PLUGIN, 'ANSIBLE_SSH_ARGS': '-o ControlMaster=no -o ControlPath=none'}
QUIET = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
# Экзитная часть стадии идёт рядом со следующими стадиями entry, которым экзиты не нужны, до названной:
# секреты экзитов забирает 20-ru-router, аварийка экзитов не нужна 25-27.
BESIDE = {'10-foreign': '20-ru-router', '23-emergency': '29-control'}


class Interrupted(SystemExit):
    """Сигнал оператора или systemd: код 130/143, перекатка портов его не глотает."""


class Deploy:
    def __init__(self, development, manifest):
        self.development, self.manifest = development, manifest
        self.tmp = Path(tempfile.mkdtemp())
        self.stages = stages.load(SITE)
        self.stage_log, self.verify_warnings, self.deploy_warnings, self.skipped = [], [], [], []
        self.mutating = self.opened = self.guard_warned = self.closing = False
        self.children, self.beat, self.beats, self.nodes = set(), None, 0, {}
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
        self.nodes = {name: {key: value['__ansible_unsafe'] if isinstance(value, dict) else value
                             for key, value in found.get('vpn2_env', {}).items()} for name, found in hosts.items()}

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

    def spawn(self, *args, stdout=None, stderr=None, env=None):
        # Деплой закрывается: потоки цепочек и пульса новых плеев не начинают.
        if self.closing and threading.current_thread() is not threading.main_thread():
            raise SystemExit(1)
        env = {**os.environ, 'ANSIBLE_CONFIG': str(INFRA / 'ansible/ansible.cfg'), 'VPN2_CONFIG': str(CONFIG), **(env or {})}
        proc = subprocess.Popen([ROOT / 'venv/bin/ansible-playbook', *map(str, args)], stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, env=env)
        self.children.add(proc)
        return proc

    def play(self, *args, **options):
        proc = self.spawn(*args, **options)
        try:
            return proc.wait()
        finally:
            if proc.poll() is None:
                proc.terminate()
            self.children.discard(proc)

    def service(self, *args, **options):
        return self.play(PLAYBOOK, '-e', f'manifest={self.manifest}', *args, **options)

    def guard_beat(self):
        """Пульс сторожу каждые 5 с новым входом, пока идёт хоть одна стадия со сторожем;
        вход на entry снаружи видят только экзиты."""
        with self.lock:
            self.beats += 1
            if self.beat:
                return
            nodes = ','.join(self.expand('all'))
            # Видят ли экзиты вход на entry, проверяет каждый пульс, пока не увидели; первый пульс даёт arm.
            if not (self.tmp / 'entry-outside').exists():
                self.service('--tags', 'beat', '--limit', nodes, '-e', f'out={self.tmp}', env=FRESH_LOGIN, **QUIET)
            if not (self.tmp / 'entry-outside').exists() and not self.guard_warned:
                self.guard_warned = True
                warn('[fw-guard] вход на entry снаружи не проверяется: до стадии его не видит ни один экзит')
            stop, current = threading.Event(), []

            def beat():
                while not stop.wait(5):
                    outside = (self.tmp / 'entry-outside').exists()
                    try:
                        current[:] = [self.spawn(PLAYBOOK, '--tags', 'beat', '--limit', nodes, '-e', f'entry_outside={outside}',
                                                 '-e', f'out={self.tmp}', env=FRESH_LOGIN, **QUIET)]
                    except SystemExit:
                        return
                    current[0].wait()
                    self.children.discard(current[0])

            self.beat = (stop, current, threading.Thread(target=beat, daemon=True))
            self.beat[2].start()

    def stop_beat(self, every=False):
        with self.lock:
            self.beats = 0 if every else self.beats - 1
            if self.beats > 0 or not self.beat:
                return
            stop, current, thread = self.beat
            self.beat = None
        stop.set()
        for proc in current:
            proc.terminate()
        thread.join()

    def guard_finish(self, stage, nodes):
        self.stop_beat()
        deadline = time.time() + GUARD_WINDOW + 30
        # Код 4 - узел не пустил: ждём окно сторожа, он вернёт фаервол, и вход появится.
        while (code := self.service('--tags', 'finish', '--limit', nodes, '-e', f'stage={stage}')) == 4 \
                and time.time() < deadline:
            time.sleep(5)
        if code == 4:
            warn(f'после стадии {stage} нода не пускает новые ssh-входы дольше окна сторожа - проверь вход руками')
        return code

    def stage_once(self, stage, nodes, env=None):
        """Одна стадия на узлах: подготовка, сторож, плей стадии, снятие сторожа."""
        log(f'[{stage} → {nodes}] starting')
        guard = stage in GUARDED
        if guard:
            self.guard_beat()
        code = self.play(PLAYBOOK, SITE, '--tags', f'prepare,{stage}', '--limit', nodes, '-e', f'stage={stage}',
                         '-e', f'manifest={self.manifest}', '-e', f'guard={guard}', env=env)
        if guard:
            code = self.guard_finish(stage, nodes) and 1 or code
        elif self.service('--tags', 'cleanup', '--limit', nodes, **QUIET):
            warn(f'[{stage} → {nodes}] не удалось убрать /tmp/anysda')
        return code

    def verify(self, nodes):
        """99-verify: 0 - всё измерено, 2 - не всё (установка годна), 1 - отказ."""
        out = Path(tempfile.mkdtemp(dir=self.tmp))
        code, result = self.stage_once('99-verify', nodes, env={'VPN2_VERIFY_OUT': str(out)}), 0
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

    def run_stage(self, stage, group):
        t0, code = time.time(), 0
        nodes = self.stage_nodes(stage, self.expand(group))
        if not nodes:
            return 0
        if stage == '00-bootstrap':
            prep_keys()
        elif stage == '20-ru-router':
            self.pull_secrets()
            self.warn_insecure_exits()
        elif stage == '23-emergency' and subprocess.run([sys.executable, '-B', '-m', 'vpn2.emergency', 'provision', REPO]).returncode:
            raise SystemExit(1)
        if stage == '10-foreign':
            if len(self.exit_tags()) == 1:
                warn('единственный экзит: на время его обновления возможен перерыв VPN')
            # Экзиты обновляются по одному, следующий - только когда предыдущий прошёл проверку.
            for host in nodes.split(','):
                if self.stage_once(stage, host) or self.verify(host):
                    code = 1
                    break
        elif stage == '99-verify':
            for host in self.skipped:
                print(f'[{host}] ⊘ пропущен: SSH недоступен в pre-flight', flush=True)
                code = 2
            code = {0: code, 2: 2}.get(self.verify(nodes), 1)
            self.warn_insecure_exits()
        elif self.stage_once(stage, nodes):
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
        """Экзитная часть стадии своим потоком; join() дожидается её и валит деплой, если она упала."""
        failed = []

        def track():
            try:
                self.run_stage(stage, 'foreign')
            except BaseException as error:
                failed.append(error)

        thread = threading.Thread(target=track, daemon=True)
        thread.start()
        return thread, failed, BESIDE[stage]

    @staticmethod
    def join(side):
        side[0].join()
        if side[1]:
            raise side[1][0]

    def do_all(self):
        report.banner(self)
        t0, verify, side = time.time(), 0, None
        self.preflight()
        try:
            for stage in self.stages:
                if side and stage.name == side[2]:
                    self.join(side)
                    side = None
                group = {'entry': 'ru', 'exits': 'foreign'}.get(stage.hosts, 'all')
                if stage.name in BESIDE:
                    side = self.beside(stage.name)
                    if group == 'foreign':
                        continue
                    group = 'ru'
                if stage.name == '35-telegram':
                    os.environ['VPN2_PANEL_FOLLOWS'] = 'true'
                if stage.name == '99-verify':
                    verify = self.run_stage(stage.name, group)
                else:
                    self.run_stage(stage.name, group)
                os.environ.pop('VPN2_PANEL_FOLLOWS', None)
                if stage.name == '20-ru-router':
                    self.rotate_ports()
        except BaseException as error:
            # Брошенная на полпути цепочка экзитов хуже ожидания; сигнал оператора не ждёт.
            if side and not isinstance(error, Interrupted):
                side[0].join()
            raise
        if self.play(SITE, '--tags', 'old-layout', '-e', 'verify_old_layout=true', '--limit', ','.join(self.expand('all')),
                     stdout=subprocess.DEVNULL):
            warn('остатки раскладки v1.1.5 не убраны: повтори ./deploy.sh')
        self.release_complete()
        report.summary(self, t0)
        report.announce(self)
        return verify

    def release_cli(self, *args):
        if release.main(['--manifest', self.manifest, *args, str(REPO)]):
            raise SystemExit(1)

    def release_start(self, action):
        """Выпуск открывается до изменений: манифест сверен с деревом, журнал прошлого деплоя закрыт,
        на каждом узле проверки и снимок. С фазы applying узлы считаются изменёнными."""
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
        bundle = f'bundle={self.tmp}/vpn2.tgz'
        if subprocess.run(['tar', '-C', INFRA / 'vpn2/src', '--exclude=__pycache__', '-czf', self.tmp / 'vpn2.tgz', 'vpn2']).returncode:
            raise SystemExit(1)
        if action == 'rollback':
            if code := self.service('--tags', 'artifacts', '--limit', 'ru', '-e', bundle, stdout=subprocess.DEVNULL):
                raise SystemExit(code)
            return
        if action != 'recover':
            with open(self.tmp / 'journal.log', 'w') as journal:
                failed = self.service('--tags', 'journal', stdout=journal, stderr=subprocess.STDOUT)
            if failed:
                found = re.search(r'unfinished deploy on [^"]*\(or \./deploy\.sh rollback\)', (self.tmp / 'journal.log').read_text())
                die(found[0] if found else 'журнал деплоя на узлах не прочитан: проверь вход по ssh')
        self.opened = True
        if self.service('--tags', 'release', '-e', bundle, '-e', f'partial={action not in ("", "recover")}'):
            raise SystemExit(1)
        self.mutating = True

    def release_complete(self):
        if self.mutating:
            if self.service('--tags', 'complete', '-e', f'bundle={self.tmp}/vpn2.tgz', stdout=subprocess.DEVNULL):
                die('запись о выпуске не закрыта: повтори ./deploy.sh recover')
            self.mutating = False

    def close(self, code):
        """Выход деплоя: кто бы ни прервал, дочерние плеи гаснут, журнал узлов честен."""
        self.closing = True
        self.stop_beat(every=True)
        for proc in list(self.children):
            with contextlib.suppress(OSError):
                proc.terminate()
        if code and self.mutating:
            print('deploy interrupted: operation journal retained; inspect services, then run ./deploy.sh recover '
                  '(or ./deploy.sh rollback)', file=sys.stderr)
        elif code and self.opened:
            self.service('--tags', 'abort', **QUIET)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def warn_insecure_exits(self):
        for host in self.exit_tags():
            if self.env().get(f'{host.upper().replace("-", "_")}_HY2_INSECURE') == '1':
                warn(f'{host}: exits[].hy2_insecure: true — сертификат экзита не проверяется, '
                     'подмену экзита на пути entry → экзит не видно')

    def pull_secrets(self):
        """Перед 20-ru-router: пароли каналов, сертификат Hysteria2, host-key и ключ Reality каждого экзита."""
        log('prep: тяну секреты Hysteria2 + clash-api с экзит-нод')
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

    def preflight(self):
        """Вход на каждый узел до изменений. Экзит без связи пропускается; сменившийся host-key и
        отказ во входе останавливают деплой: это чинит оператор."""
        step('pre-flight: доступность SSH')
        out = Path(tempfile.mkdtemp(dir=self.tmp))
        with open(out / 'log', 'w') as journal:
            code = self.service('--tags', 'preflight', '-e', f'out={out}', stdout=journal, stderr=journal, env=SSH_PLUGIN)
        reasons, fixes, auth, net = [], [], False, False
        for host in self.expand('all'):
            if not (out / f'{host}.fail').is_file():
                if code:
                    print(''.join((out / 'log').read_text().splitlines(True)[-20:]), end='', file=sys.stderr)
                    die('pre-flight: проверка входа не выполнилась, причина выше')
                print(f'  {host:<30} {G}✓{E}', flush=True)
                continue
            addr, reason = self.env(host).get('SSH_HOST', ''), (out / f'{host}.fail').read_text().replace('\n', ' ')
            if 'IDENTIFICATION HAS CHANGED' in reason or 'Host key verification failed' in reason:
                fixes.append(f'ssh-keygen -R {addr} -f {STATE_DIR}/known_hosts')
            elif 'Permission denied' in reason or 'Authentication failed' in reason:
                auth = True
            elif host != 'ru':
                self.skipped.append(host)
                print(f'  {Y}⚠ Экзит {host} пропущен: SSH недоступен в pre-flight{E}', flush=True)
                continue
            else:
                net = True
            print(f'  {host:<30} {R}✗{E}', flush=True)
            reasons.append(f'нода {host} ({addr}) недоступна по ssh с entry: {reason}')
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
            lines += ['Нода отказала во входе:',
                      '  1. ключ anysda-orchestrator в /root/.ssh/authorized_keys на ноде: после 00-bootstrap экзит пускает только по нему',
                      '  2. пароль root в config.yaml', '  3. PasswordAuthentication=yes в /etc/ssh/sshd_config на ноде',
                      '  4. если ОС на ноде только что переустановили, пароль мог ещё не выставиться: повтори ./deploy.sh через минуту']
        if net:
            lines += ['Проверь:', '  1. IP ноды в config.yaml', '  2. sshd на ноде запущен и слушает :22',
                      '  3. firewall / geo-блок на стороне провайдера ноды']
        print(f'\n{R}✗ Недоступны по SSH:{E}\n' + ''.join(f'  {line}\n' for line in lines), end='', flush=True)
        die(f'pre-flight: недоступных по ssh нод: {len(reasons)}, деплой не запускался')


def install(src, dst, mode):
    shutil.copyfile(src, dst)
    dst.chmod(mode)
