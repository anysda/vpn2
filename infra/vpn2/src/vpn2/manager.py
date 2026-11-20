"""Менеджер установки: служба root на entry с закрытым набором действий над установкой и составом экзитов.

API 1 - одна строка JSON на соединение с /run/vpn2-manager/manager.sock (как у vpn2-control).
Запрос {api: 1, method, params} - params ровно такие, как ниже; ответ {api: 1, ok: true, result} или
{api: 1, ok: false, error: validation|forbidden}. Отказ действия - ok: true, result {refused: текст для человека}.
  exit.check {tag, host}  -> {problems: [текст]}; пусто - экзит годен: вход root по ключу оркестратора, Ubuntu 26.04
  install.run {}          -> задание: полная установка по config.yaml (то же, что ./deploy.sh -yaml)
  exit.add {tag, host}    -> задание: экзит в работающую систему
  exit.remove {tag}       -> задание: экзит из маршрутов, мониторинга, config.yaml и наши службы с самого сервера
  job.read {job}          -> задание; job 0 - последнее
  job.retry {job}         -> задание повторяется (упавшее или прерванное)
  job.skip {job, tag}     -> установка продолжается без этого экзита
  job.forget {job}        -> задание закрыто; у упавшего добавления убирается то, что успело лечь
  status.read {}          -> {deploy: idle|busy, job: задание|null, nodes: [{tag, role: entry|exit, host, monitoring: up|down|unknown}]}
host - адрес или root@адрес, tag - тег экзита, job - номер задания; путей, команд и прочих аргументов нет.
Задание: {id, kind: install|add|remove, tag, host, state: running|done|failed|interrupted|forgotten,
  stages: [{name, state: pending|running|done|failed, nodes, failed}], log: журнал текущей или упавшей стадии
  (до 64 КиБ с конца), error: строка отказа, actions: [retry, skip, forget], skipped: [тег], result}.
result удаления: {exit, server: cleaned|partial|forgotten, left: [что осталось на сервере]}.

Одно задание за раз, замок общий с ./deploy.sh. Задание идёт своей службой systemd (vpn2-manager-job): закрытая
вкладка, перезапуск менеджера и деплой самого менеджера его не обрывают. Состояние на диске; задание, чей процесс
пропал (kill, перезагрузка), - interrupted, config.yaml возвращён к составу до задания.
"""
import contextlib
import json
import os
import pwd
import re
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

from vpn2 import config, fsutil, nodevars

API, LIMIT, LOG_TAIL = 1, 1024 * 1024, 64 * 1024
STATE = Path(os.environ.get('VPN2_MANAGER_STATE') or '/var/lib/vpn2-manager')
SOCKET = Path(os.environ.get('VPN2_MANAGER_SOCKET') or '/run/vpn2-manager/manager.sock')
LOCK = Path(os.environ.get('VPN2_DEPLOY_LOCK') or '/run/lock/anysda-vpn2-deploy.lock')
UNIT = 'vpn2-manager-job'
METHODS = {'exit.check': 'tag host', 'install.run': '', 'exit.add': 'tag host', 'exit.remove': 'tag',
           'job.read': 'job', 'job.retry': 'job', 'job.skip': 'job tag', 'job.forget': 'job', 'status.read': ''}
OPEN = ('running', 'failed', 'interrupted')
RECAP = re.compile(r'(?m)^(\S+)\s+: ok=\d+\s+changed=\d+\s+unreachable=(\d+)\s+failed=(\d+)')


class Invalid(Exception):
    """Запрос не по API: ответ error validation."""


class Refused(Exception):
    """Действие не выполняется: текст - человеку."""


def validate(request):
    """(method, params) из запроса; всё, что не по API, - Invalid. Смысл тега и адреса проверяет действие."""
    if type(request) is not dict or set(request) != {'api', 'method', 'params'} or request['api'] != API:
        raise Invalid
    method, params = request['method'], request['params']
    if method not in METHODS or type(params) is not dict or set(params) != set(METHODS[method].split()):
        raise Invalid
    for name, value in params.items():
        if name == 'job' and not (type(value) is int and 0 <= value < 2**31) or name != 'job' and not (
                type(value) is str and re.fullmatch(r'[!-~]{1,%d}' % (64 if name == 'tag' else 260), value)):
            raise Invalid
    return method, params


def failure(said, host, body):
    """Строка отказа из журнала: сообщение деплоя или упавшая задача ansible на узле (её stderr, иначе msg)."""
    if not host:
        return said
    try:
        found = json.loads(body)
    except ValueError:
        return f'{host}: {body[:300]}'
    return f'{host}: {(found.get("stderr") or found.get("msg") or "").strip()[:300]}'


class Progress:
    """Ход задания: пишет процесс деплоя (VPN2_JOB - каталог задания), читает менеджер."""

    def __init__(self, folder):
        self.path, self.log, self.lock, self.active = folder / 'progress.json', folder / 'log', threading.Lock(), {}
        self.data = fsutil.read_json(self.path, {}) if self.path.exists() else {}
        self.data = {'stages': [], **self.data, 'attempt': self.data.get('attempt', 0) + 1}
        self.data.pop('exit', None)

    @classmethod
    def from_env(cls):
        return cls(Path(os.environ['VPN2_JOB'])) if os.environ.get('VPN2_JOB') else None

    def write(self):
        fsutil.write_json(self.path, self.data)

    def record(self, name):
        found = next((item for item in self.data['stages'] if item['name'] == name), None)
        if not found:
            found = {'name': name, 'state': 'pending', 'nodes': [], 'failed': []}
            self.data['stages'].append(found)
        return found

    def plan(self, names):
        with self.lock:
            self.data['stages'] = [{'name': name, 'state': 'pending', 'nodes': [], 'failed': []} for name in names]
            self.write()

    @contextlib.contextmanager
    def stage(self, name, nodes):
        """Стадия идёт; части одной стадии (entry и экзиты разом) - одна строка хода."""
        sys.stdout.flush()
        sys.stderr.flush()
        with self.lock:
            found = self.record(name)
            self.active[name] = self.active.get(name, 0) + 1
            if self.active[name] == 1:
                found.update(state='running', failed=[], offset=self.log.stat().st_size if self.log.exists() else 0)
            found['nodes'] = sorted(set(found['nodes']) | set(nodes))
            self.write()
        try:
            yield found
        except BaseException:
            sys.stdout.flush()
            with self.lock:
                self.active[name] -= 1
                found.update(state='failed', failed=found['failed'] or self.recap(found['offset']))
                self.write()
            raise
        with self.lock:
            self.active[name] -= 1
            if not self.active[name] and found['state'] != 'failed':
                found.update(state='done', failed=[])
            self.write()

    def recap(self, offset):
        """Узлы с ошибкой по итогу плея (PLAY RECAP) в журнале стадии."""
        with contextlib.suppress(OSError), open(self.log, 'rb') as log:
            log.seek(offset)
            return sorted({host for host, lost, failed in RECAP.findall(log.read().decode(errors='replace'))
                           if int(lost) or int(failed)})
        return []

    def finish(self, code, result):
        with self.lock:
            self.data.update(exit=code, result=result)
            self.write()


class Manager:
    def __init__(self, state=STATE, config_path=None, lock=LOCK, repo=None):
        from vpn2.deploy import CONFIG, REPO
        self.jobs, self.config, self.lock = state / 'jobs', Path(config_path or CONFIG), lock
        self.repo, self.mutex = Path(repo or REPO), threading.Lock()
        self.journal = Path(os.environ.get('VPN2_DEPLOY_JOURNAL') or '/var/lib/vpn2-deploy/operation.json')

    # Процесс задания - служба systemd: менеджер его не держит.
    def alive(self):
        return subprocess.run(['systemctl', 'is-active', '--quiet', UNIT]).returncode == 0

    def spawn(self, folder, argv, recover):
        log = folder / 'log'
        subprocess.run(['systemd-run', f'--unit={UNIT}', '--collect', '--quiet', '-p', f'StandardOutput=append:{log}',
                        '-p', f'StandardError=append:{log}', f'--setenv=VPN2_JOB={folder}',
                        f'--setenv=VPN2_RECOVER={int(recover)}', str(self.repo / 'deploy.sh'), *argv],
                       check=True, capture_output=True)

    def journal_open(self):
        """Прошлый деплой упал после начала изменений: журнал выпуска entry не закрыт."""
        found = fsutil.read_json(self.journal, {}) if self.journal.is_file() else {}
        return bool(found) and found.get('phase') not in ('complete', 'aborted', 'rolled-back')

    def busy(self):
        try:
            with fsutil.lock(self.lock, blocking=False):
                return False
        except fsutil.LockBusy:
            return True

    def folder(self, number):
        return self.jobs / str(number)

    def latest(self):
        numbers = [int(path.name) for path in self.jobs.glob('*') if path.name.isdigit()] if self.jobs.is_dir() else []
        return max(numbers, default=0)

    def load(self, number):
        number = number or self.latest()
        path = self.folder(number) / 'job.json'
        return self.settle(fsutil.read_json(path)) if number and path.is_file() else None

    def save(self, job):
        fsutil.write_json(self.folder(job['id']) / 'job.json', job)

    def settle(self, job):
        """Задание, чей процесс кончился: итог из хода; не done - config.yaml к составу до задания."""
        if job['state'] != 'running' or self.alive():
            return job
        code = fsutil.read_json(self.folder(job['id']) / 'progress.json', {}).get('exit')
        job['state'] = 'done' if code in (0, 2) else 'failed' if code is not None else 'interrupted'
        if job['state'] != 'done':
            self.restore(job)
        self.save(job)
        return job

    def restore(self, job):
        """config.yaml до задания; копия ключа оркестратора, что деплой дописал за задание, остаётся."""
        original, current = (self.folder(job['id']) / 'config.orig').read_text(), self.config.read_text()
        if current == original:
            return
        keys = [line for line in current.splitlines() if re.match(r'orchestrator_(pub)?key:', line) and line not in original]
        config.replace(self.config, original.rstrip('\n') + ''.join(f'\n{line}' for line in keys) + '\n')

    def exits(self, text=None):
        return [(node.tag, node.host) for node in config.loads(text or self.config.read_text()).exits]

    def run(self, job, recover=False):
        """Состав экзитов задания в config.yaml и процесс задания. recover - продолжение после своего упавшего
        задания: журнал выпуска, что оно оставило открытым, доводится, как у ./deploy.sh recover."""
        folder = self.folder(job['id'])
        original = self.config.read_text()
        (folder / 'config.orig').write_text(original)
        (folder / 'config.orig').chmod(0o600)
        exits = self.exits(original) + ([(job['tag'], job['host'])] if job['kind'] == 'add' else [])
        wanted = [item for item in exits if item[0] not in job['skipped'] and not (job['kind'] == 'remove' and item[0] == job['tag'])]
        if wanted != self.exits(original):
            config.set_exits(self.config, wanted)
        argv = {'install': ['recover'] if recover else [], 'add': ['exit-add', job['tag']], 'remove': ['exit-remove', job['tag'], job['host']]}[job['kind']]
        # Итог прошлой попытки не должен достаться новой, что упала до первой записи хода.
        if (folder / 'progress.json').exists():
            fsutil.write_json(folder / 'progress.json', {**fsutil.read_json(folder / 'progress.json'), 'exit': None})
        job['state'] = 'running'
        self.save(job)
        try:
            self.spawn(folder, argv, recover)
        except (OSError, subprocess.CalledProcessError) as error:
            with open(folder / 'log', 'a') as log:
                log.write(f'[manager] FATAL: задание не запустилось: {getattr(error, "stderr", "") or error}\n')
            job['state'] = 'failed'
            self.restore(job)
            self.save(job)
        return self.view(job)

    def start(self, kind, tag='', host=''):
        last = self.load(0)
        if last and last['state'] in OPEN:
            raise Refused(f'идёт задание {last["id"]}' if last['state'] == 'running' else
                          f'задание {last["id"]} не закрыто: повтори его или забудь')
        if self.busy():
            raise Refused('идёт ./deploy.sh: дождись его конца')
        if self.journal_open():
            raise Refused('прошлый ./deploy.sh упал на середине: сначала ./deploy.sh recover')
        if not self.config.is_file():
            raise Refused('config.yaml не заполнен')
        if kind == 'add' and (problems := self.problems(tag, host)):
            raise Refused('; '.join(problems))
        if kind == 'remove':
            found = dict(self.exits())
            if tag not in found:
                raise Refused(f'экзита {tag} нет в config.yaml')
            if len(found) == 1:
                raise Refused('последний экзит удалить нельзя: без экзита VPN не работает')
            host = found[tag]
        job = {'id': self.latest() + 1, 'kind': kind, 'tag': tag, 'host': config._exit_host(host, 'host') if host else '',
               'state': 'new', 'skipped': []}
        self.folder(job['id']).mkdir(parents=True, mode=0o700)
        return self.run(job)

    def closed(self, number):
        job = self.load(number)
        if not job:
            raise Refused(f'нет задания {number}')
        if job['state'] not in ('failed', 'interrupted') or job['id'] != self.latest():
            raise Refused(f'задание {job["id"]}: {job["state"]}, повтор, пропуск и «забыть» - только у последнего упавшего')
        if self.busy():
            raise Refused('идёт ./deploy.sh: дождись его конца')
        return job

    def retry(self, number):
        return self.run(self.closed(number), recover=True)

    def skip(self, number, tag):
        job = self.closed(number)
        exits = [name for name, _host in self.exits() if name not in job['skipped']]
        if job['kind'] != 'install':
            raise Refused('без экзита продолжается только установка; добавление - «забыть»')
        if tag not in exits:
            raise Refused(f'экзита {tag} нет в установке')
        if len(exits) == 1:
            raise Refused('без последнего экзита установка не продолжается')
        job['skipped'].append(tag)
        return self.run(job, recover=True)

    def forget(self, number):
        job = self.closed(number)
        job['state'] = 'forgotten'
        self.save(job)
        stages = fsutil.read_json(self.folder(job['id']) / 'progress.json', {}).get('stages', [])
        if job['kind'] != 'add' or all(item['state'] == 'pending' or item['name'] == 'preflight' for item in stages):
            return self.view(job)
        # Добавление успело что-то положить: entry перестраивается без экзита, сервер чистится.
        cleanup = {'id': self.latest() + 1, 'kind': 'remove', 'tag': job['tag'], 'host': job['host'], 'state': 'new', 'skipped': []}
        self.folder(cleanup['id']).mkdir(parents=True, mode=0o700)
        return self.run(cleanup, recover=True)

    def problems(self, tag, host):
        """Отказы до любых изменений: формат, занятость, сама entry; затем вход по ssh и ОС."""
        cfg, out = config.loads(self.config.read_text()), []
        if not nodevars.TAG.fullmatch(tag) or tag in nodevars.RESERVED_TAGS or len(tag) > 63:
            out.append(f'тег «{tag}»: строчные латинские буквы, цифры и дефис, с буквы; не ru, all, exits, foreign')
        elif tag in {node.tag for node in cfg.exits}:
            out.append(f'тег {tag} уже занят')
        try:
            address = config._exit_host(host, 'host')
            nodevars._check_host(address, 'host')
        except config.ConfigError as error:
            return [*out, f'адрес {host}: {error.reason}']
        seen = resolve(address)
        used = [node.tag for node in cfg.exits if node.host == address or resolve(node.host) & seen]
        if used:
            out.append(f'адрес {address} уже у экзита {used[0]}')
        if address == cfg.entry.host or seen & (local_addresses() | resolve(cfg.entry.host)):
            out.append(f'{address} - это сама entry')
        return out or self.login_problems(address)

    def login_problems(self, address):
        from vpn2.deploy import orchestrator_key, ssh_options
        from vpn2.deploy.run import key_text, refused
        command = ['ssh', '-o', 'BatchMode=yes', *ssh_options(), '-i', str(orchestrator_key()), f'root@{address}',
                   'id -u; . /etc/os-release && echo "$ID $VERSION_ID"']
        try:
            done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            return [f'{address} не ответил по ssh за 20 с']
        said = ' '.join(line for line in done.stderr.splitlines() if not line.startswith('Warning: Permanently added'))
        if done.returncode == 255 and ('IDENTIFICATION HAS CHANGED' in said or 'Host key verification failed' in said):
            return [f'у {address} сменился ключ хоста (переустановка ОС?): если это твой сервер - '
                    f'ssh-keygen -R {address} -f {config.STATE_DIR}/known_hosts и повтори']
        if done.returncode == 255 and refused(said):
            return [f'{address} не пускает по ключу оркестратора: положи в /root/.ssh/authorized_keys строку {key_text()}']
        if done.returncode == 255:
            return [f'{address} не отвечает по ssh: {said.strip() or "нет связи"}']
        lines = done.stdout.split()
        if lines[:1] != ['0']:
            return [f'{address}: вход не под root']
        if lines[1:] != ['ubuntu', '26.04']:
            return [f'на {address} {" ".join(lines[1:]) or "неизвестная ОС"}: нужна Ubuntu 26.04']
        return []

    def view(self, job):
        folder = self.folder(job['id'])
        progress = fsutil.read_json(folder / 'progress.json', {}) if (folder / 'progress.json').exists() else {}
        stages = progress.get('stages', [])
        current = next((item for item in reversed(stages) if item['state'] in ('running', 'failed')), None)
        log = b''
        with contextlib.suppress(OSError), open(folder / 'log', 'rb') as handle:
            handle.seek(max(current.get('offset', 0) if current else 0, handle.seek(0, 2) - LOG_TAIL))
            log = handle.read()
        text = log.decode(errors='replace')
        errors = [failure(*found) for found in re.findall(r'(?m)(?:FATAL:|^✗)\s*(.+)$|^fatal: \[(\S+)\]: FAILED! => (.+)$', text)]
        open_job = job['state'] in ('failed', 'interrupted')
        return {**{name: job[name] for name in ('id', 'kind', 'tag', 'host', 'state', 'skipped')},
                # Оборванное задание не оставляет стадий «идёт».
                'stages': [{**{name: item[name] for name in ('name', 'nodes', 'failed')}, 'state': 'failed' if (
                    item['state'] == 'running' and job['state'] != 'running') else item['state']} for item in stages],
                'log': text, 'error': errors[-1] if errors and open_job else '',
                'actions': ['retry', *(['skip'] if job['kind'] == 'install' else []), 'forget'] if open_job else [],
                'result': progress.get('result')}

    def status(self):
        from vpn2.containers import targets
        cfg = config.loads(self.config.read_text()) if self.config.is_file() else None
        found = {(item.get('labels') or {}).get('host'): item.get('health')
                 for item in (targets().get('data') or {}).get('activeTargets') or []}
        nodes = [{'tag': node.tag, 'role': role, 'host': node.host, 'monitoring': found.get(node.tag) or 'unknown'}
                 for role, items in (('entry', [cfg.entry]), ('exit', cfg.exits)) if cfg for node in items]
        job = self.load(0)
        return {'deploy': 'busy' if self.busy() else 'idle', 'job': job and self.view(job), 'nodes': nodes}

    def execute(self, method, params):
        if method == 'exit.check':
            return {'problems': self.problems(params['tag'], params['host'])}
        if method == 'status.read':
            return self.status()
        with self.mutex:
            if method == 'job.read':
                job = self.load(params['job'])
                if not job:
                    raise Refused(f'нет задания {params["job"]}')
                return self.view(job)
            if method == 'install.run':
                return self.start('install')
            if method in ('exit.add', 'exit.remove'):
                return self.start(method.removeprefix('exit.'), params['tag'], params.get('host', ''))
            if method == 'job.skip':
                return self.skip(params['job'], params['tag'])
            return getattr(self, method.removeprefix('job.'))(params['job'])

    def watch(self):
        """Задание кончилось без читателя: итог и возврат config.yaml сразу, а не при первом чтении."""
        while True:
            with self.mutex, contextlib.suppress(OSError, ValueError):
                self.load(0)
            threading.Event().wait(5)


def resolve(host):
    with contextlib.suppress(OSError, UnicodeError):
        return {item[4][0] for item in socket.getaddrinfo(host, None, socket.AF_INET)}
    return set()


def local_addresses():
    found = subprocess.run(['ip', '-4', '-o', 'addr', 'show'], capture_output=True, text=True).stdout
    return set(re.findall(r'inet (\S+?)/', found))


def answer(manager, line):
    try:
        method, params = validate(json.loads(line))
        try:
            return {'api': API, 'ok': True, 'result': manager.execute(method, params)}
        except Refused as refusal:
            return {'api': API, 'ok': True, 'result': {'refused': str(refusal)}}
    except (Invalid, ValueError, TypeError, RecursionError):
        return {'api': API, 'ok': False, 'error': 'validation'}


def callers():
    """root и служба vpn2-control: через неё ходят панель и мастер установки."""
    with contextlib.suppress(KeyError):
        return {0, pwd.getpwnam('vpn2-control').pw_uid}
    return {0}


def serve(manager, path=SOCKET):
    """Сокет появляется под своим именем, уже слушая и с правами: клиент не застаёт его полуготовым."""
    path.parent.mkdir(mode=0o755, exist_ok=True)
    new = path.with_name(f'.{path.name}.new')
    new.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(new))
        with contextlib.suppress(KeyError):
            os.chown(new, 0, pwd.getpwnam('vpn2-control').pw_gid)
        new.chmod(0o660)
        listener.listen(16)
        os.replace(new, path)
        threading.Thread(target=manager.watch, daemon=True).start()
        while True:
            connection, _address = listener.accept()
            with connection:
                connection.settimeout(3)
                uid = struct.unpack('iII', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                                 struct.calcsize('iII')))[1]
                with contextlib.suppress(OSError):
                    line = connection.makefile('rb').readline(LIMIT)
                    reply = answer(manager, line) if uid in callers() else {'api': API, 'ok': False, 'error': 'forbidden'}
                    connection.sendall(json.dumps(reply, ensure_ascii=False).encode() + b'\n')


def call(method, params, path=SOCKET, timeout=60):
    """Один запрос к менеджеру; ответ целиком."""
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(json.dumps({'api': API, 'method': method, 'params': params}).encode() + b'\n')
        return json.loads(connection.makefile('rb').readline(LIMIT))


def main(argv):
    """vpn2 manager serve | vpn2 manager <метод> [params JSON]: запрос к службе, ответ - JSON в stdout."""
    if argv == ['serve']:
        serve(Manager())
        return 0
    if len(argv) not in (1, 2) or argv[0] not in METHODS:
        print(main.__doc__, file=sys.stderr)
        return 2
    reply = call(argv[0], json.loads(argv[1]) if len(argv) == 2 else {})
    print(json.dumps(reply, ensure_ascii=False, indent=2))
    return 0 if reply.get('ok') and 'refused' not in reply.get('result', {}) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
