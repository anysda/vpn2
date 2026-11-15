"""Плеи деплоя из одного прогретого процесса ansible: импорт и разбор плейбуков с ролями - раз за запуск.

Каждый плей - fork прогретого процесса, внутри - консольный ansible-playbook venv с теми же аргументами,
окружением и stdout/stderr, что дал бы ему Popen; дескрипторы вывода приходят через сокет.
Клиент (Plays) идёт системным python3 деплоя, процесс плеев - python3 из venv (запуск этого файла).
"""
import atexit
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path


class Plays:
    def __init__(self, root, env):
        self.sock, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.script = str(Path(root) / 'venv/bin/ansible-playbook')
        self.proc = subprocess.Popen([Path(root) / 'venv/bin/python3', '-B', __file__, str(theirs.fileno()), self.script],
                                     pass_fds=[theirs.fileno()], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, env=env)
        theirs.close()
        self.waiting, self.lock, self.count = {}, threading.Lock(), 0
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        with contextlib.suppress(OSError):
            while message := self.sock.recv(4096):
                found = json.loads(message)
                with self.lock:
                    entry = self.waiting.get(found['id'], {})
                    entry.update(found)
                if 'code' in found and entry:
                    entry['done'].set()
        with self.lock:
            for entry in self.waiting.values():
                entry.setdefault('code', 1)
                entry['done'].set()

    def run(self, args, env, stdout=None, stderr=None):
        """Код плея как у Popen.wait(); прерванное ожидание гасит плей."""
        files = [open(os.devnull, 'w') if out == subprocess.DEVNULL else out for out in (stdout, stderr)]
        fds = [file if isinstance(file, int) else (fd if file is None else file.fileno())
               for fd, file in zip((1, 2), files)]
        with self.lock:
            self.count += 1
            key, entry = self.count, {'done': threading.Event()}
            self.waiting[key] = entry
            try:
                socket.send_fds(self.sock, [json.dumps({'id': key, 'args': args, 'env': env}).encode()], fds)
            except OSError:
                entry['code'] = 1
                entry['done'].set()
        try:
            entry['done'].wait()
            return entry['code']
        finally:
            for file, given in zip(files, (stdout, stderr)):
                if given == subprocess.DEVNULL:
                    file.close()
            if 'code' not in entry and 'pid' in entry:
                kill(entry['pid'])
            with self.lock:
                self.waiting.pop(key, None)

    def stop(self):
        """Идущие плеи гаснут, процесс плеев уходит сам, увидев закрытый сокет."""
        with self.lock:
            for entry in self.waiting.values():
                if 'pid' in entry:
                    kill(entry['pid'])

    def close(self):
        self.stop()
        # shutdown, не только close: recv читателя держит сокет, и процесс плеев иначе не увидит конца.
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        self.proc.wait()


def kill(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def warm(infra):
    """Импорт ansible и YAML плейбуков с ролями - в кэш, что наследует каждый fork."""
    try:
        import ansible.cli.playbook  # noqa: F401
        from ansible.errors import AnsibleError
        from ansible.parsing.dataloader import DataLoader
    except ImportError:
        return {}
    loader = DataLoader()
    for path in [infra / 'deploy.yml', infra / 'site.yml', *sorted(infra.glob('roles/*/*/*.yml'))]:
        # Битый или пропавший файл здесь не ошибка: о нём скажет сам плей.
        if (path.parent == infra or path.parent.name in ('tasks', 'handlers', 'defaults', 'vars', 'meta')) and path.is_file():
            with contextlib.suppress(AnsibleError):
                loader.load_from_file(str(path))
    cache, init = loader._FILE_CACHE, DataLoader.__init__

    def shared(self):
        init(self)
        self._FILE_CACHE = cache
    DataLoader.__init__ = shared
    return cache


def only_tagged(cache, site, args):
    """Плеи site.yml, чей тег не назван в --tags, ansible пропустил бы целиком (тегов задач в ролях нет):
    их роли не разбираются вовсе."""
    if '--tags' in args and str(site) in cache:
        wanted = set(args[args.index('--tags') + 1].split(','))
        cache[str(site)] = [found for found in cache[str(site)] if wanted & set(found.get('tags', ()))]


def play(sock, script, request, fds, cache, site):
    """В fork: консольный ansible-playbook как отдельный процесс, код - кодом выхода."""
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    # На выходе - только свои atexit плея (Mitogen гасит мультиплексоры): общие остаются процессу плеев.
    atexit._clear()
    sock.close()
    null = os.open(os.devnull, os.O_RDONLY)
    for fd, target in ((null, 0), *zip(fds, (1, 2))):
        if fd != target:
            os.dup2(fd, target)
            os.close(fd)
    os.environ.clear()
    os.environ.update(request['env'])
    sys.argv = [script, *request['args']]
    only_tagged(cache, site, request['args'])
    code = 0
    try:
        if 'ansible.cli' in sys.modules:
            # Проверки и цвет, что ansible делает при импорте, - по выводу этого плея.
            import importlib

            import ansible.cli
            import ansible.utils.color
            from ansible.utils.display import Display
            ansible.cli.check_blocking_io()
            importlib.reload(ansible.utils.color)
            Display()._set_column_width()
        import runpy
        runpy.run_path(script, run_name='__main__')
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 0 if error.code is None else 1
        if isinstance(error.code, str):
            print(error.code, file=sys.stderr)
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    atexit._run_exitfuncs()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def serve(fd, script):
    # Сигнал оператора или systemd получают сами плеи и деплой; деплой ещё играет abort,
    # процесс плеев уходит с закрытием сокета.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sock = socket.socket(fileno=fd)
    infra = Path(os.environ['ANSIBLE_CONFIG']).parent
    cache = warm(infra)
    plays = {}  # pidfd -> (id, pid)

    def say(**reply):
        with contextlib.suppress(OSError):
            sock.send(json.dumps(reply).encode())
    import select
    while True:
        for ready in select.select([sock, *plays], [], [])[0]:
            if ready is not sock:
                key, pid = plays.pop(ready)
                os.close(ready)
                say(id=key, code=os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
                continue
            message, fds, _flags, _addr = socket.recv_fds(sock, 1 << 20, 2)
            if not message:
                for _key, pid in plays.values():
                    kill(pid)
                return
            request = json.loads(message)
            pid = os.fork()
            if pid == 0:
                for other in plays:
                    os.close(other)
                play(sock, script, request, fds, cache, infra / 'site.yml')
            for passed in fds:
                os.close(passed)
            plays[os.pidfd_open(pid)] = request['id'], pid
            say(id=request['id'], pid=pid)

if __name__ == '__main__':
    serve(int(sys.argv[1]), sys.argv[2])
