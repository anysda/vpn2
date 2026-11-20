"""Подставной менеджер установки для e2e: API vpn2.manager на его сокете, задания без деплоя.

Сценарий задаёт ввод: тег fail* - добавление падает на маршрутах entry (повтор проходит), cut* - обрывается;
адрес на .66 - проверка с двумя замечаниями; удаление экзита с адресом на .4 - сервер не ответил (forgotten),
на .5 - очищен не весь (partial). Каждое чтение задания продвигает его на стадию; файл hold держит ход и тянет проверку 2 с,
файл deploy-busy - «идёт ./deploy.sh». Законченное задание меняет runtime/nodes.json, как стадия 30-frontend.
"""
import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, '/usr/local/lib/vpn2-control')
from vpn2.manager import Invalid, validate  # noqa: E402

RUN, NODES = Path('/run/vpn2-manager'), Path('/var/lib/anysda-vpn2/runtime/nodes.json')
ADD = ['preflight', '00-bootstrap', '05-mgmt-mesh', '10-foreign', '20-ru-router', '23-emergency', '25-monitoring',
       '30-frontend', '99-verify']
REMOVE = [*ADD[4:], 'cleanup']
jobs = []


def nodes():
    return json.loads(NODES.read_text())


def write_nodes(items):
    NODES.with_suffix('.new').write_text(json.dumps(items))
    os.replace(NODES.with_suffix('.new'), NODES)


def view(job):
    open_job = job['state'] in ('failed', 'interrupted')
    stages = [{'name': name, 'nodes': [job['tag']] if i < 4 and job['kind'] == 'add' else ['ru'],
               'failed': ['ru'] if job['state'] == 'failed' and i == job['at'] else [],
               'state': 'done' if i < job['at'] else 'failed' if i == job['at'] and open_job
               else 'running' if i == job['at'] and job['state'] == 'running' else 'pending'}
              for i, name in enumerate(ADD if job['kind'] == 'add' else REMOVE)]
    failed = job['state'] == 'failed'
    return {**{k: job[k] for k in ('id', 'kind', 'tag', 'host', 'state', 'skipped', 'result')}, 'stages': stages,
            'log': f'TASK [router : sing-box config]\nfatal: [ru]: FAILED! => {{"msg": "sing-box check: {job["tag"]}"}}\n'
                   if failed else f'[ru] стадия {stages[min(job["at"], len(stages) - 1)]["name"]}\n',
            'error': f'ru: sing-box check: {job["tag"]}' if failed else '',
            'actions': ['retry', 'forget'] if open_job else []}


def step(job):
    """Одна стадия вперёд; на последней - итог и новый состав в nodes.json."""
    plan = ADD if job['kind'] == 'add' else REMOVE
    if job['state'] != 'running' or (RUN / 'hold').exists():
        return
    if job['tag'].startswith('fail') and job['at'] == 4 and not job['retried']:
        job['state'] = 'failed'
        return
    if job['tag'].startswith('cut') and job['at'] == 2 and not job['retried']:
        job['state'] = 'interrupted'
        return
    job['at'] += 1
    if job['at'] < len(plan):
        return
    job['state'], items = 'done', nodes()
    if job['kind'] == 'add':
        write_nodes([*items, {'tag': job['tag'], 'host': job['host'], 'mgmt': f'10.99.0.{len(items) + 1}'}])
    else:
        write_nodes([n for n in items if n['tag'] != job['tag']])
        server = {'4': 'forgotten', '5': 'partial'}.get(job['host'].rsplit('.', 1)[-1], 'cleaned')
        job['result'] = {'exit': job['tag'], 'server': server, 'left': ['/etc/sing-box: каталог не пуст'] if server == 'partial' else []}


def start(kind, tag, host):
    last = jobs[-1] if jobs else None
    if last and last['state'] in ('running', 'failed', 'interrupted'):
        return {'refused': f'идёт задание {last["id"]}' if last['state'] == 'running' else f'задание {last["id"]} не закрыто: повтори его или забудь'}
    if (RUN / 'deploy-busy').exists():
        return {'refused': 'идёт ./deploy.sh: дождись его конца'}
    exits = {n['tag']: n['host'] for n in nodes() if n['tag'] != 'ru'}
    if kind == 'add' and (found := problems(tag, host)):
        return {'refused': '; '.join(found)}
    if kind == 'remove':
        if tag not in exits:
            return {'refused': f'экзита {tag} нет в config.yaml'}
        if len(exits) == 1:
            return {'refused': 'последний экзит удалить нельзя: без экзита VPN не работает'}
        host = exits[tag]
    jobs.append({'id': len(jobs) + 1, 'kind': kind, 'tag': tag, 'host': host.removeprefix('root@'), 'state': 'running',
                 'skipped': [], 'result': None, 'at': 0, 'retried': False})
    return view(jobs[-1])


def problems(tag, host):
    address, exits = host.removeprefix('root@'), [n for n in nodes() if n['tag'] != 'ru']
    found = [f'тег {tag} уже занят'] if any(n['tag'] == tag for n in exits) else []
    found += [f'адрес {address} уже у экзита {n["tag"]}' for n in exits if n['host'] == address]
    if address.endswith('.66'):
        found += [f'{address} не отвечает по ssh: ssh: connect to host {address} port 22: No route to host',
                  f'на {address} debian 13: нужна Ubuntu 26.04']
    return found


def execute(method, params):
    if method == 'status.read':
        return {'deploy': 'busy' if (RUN / 'deploy-busy').exists() or (jobs and jobs[-1]['state'] == 'running') else 'idle',
                'job': view(jobs[-1]) if jobs else None,
                'nodes': [{'tag': n['tag'], 'role': 'entry' if n['tag'] == 'ru' else 'exit', 'host': n['host'], 'monitoring': 'up'}
                          for n in nodes()]}
    if method == 'key.read':
        return {'key': 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE2eE2eE2eE2eE2eE2eE2eE2eE2eE2eE2eE2eE2eE2e root@entry'}
    if method == 'exit.check':
        if (RUN / 'hold').exists():
            time.sleep(2)
        return {'problems': problems(params['tag'], params['host'])}
    if method in ('exit.add', 'exit.remove'):
        return start(method.removeprefix('exit.'), params['tag'], params.get('host', ''))
    job = next((j for j in jobs if j['id'] == params.get('job')), jobs[-1] if jobs and params.get('job') == 0 else None)
    if not job:
        return {'refused': f'нет задания {params.get("job")}'}
    if method == 'job.read':
        step(job)
    elif job['state'] not in ('failed', 'interrupted') or job is not jobs[-1]:
        return {'refused': f'задание {job["id"]}: {job["state"]}, повтор, пропуск и «забыть» - только у последнего упавшего'}
    elif method == 'job.retry':
        job.update(state='running', retried=True)
    elif method == 'job.forget':
        job['state'] = 'forgotten'
    else:
        return {'refused': 'без экзита продолжается только установка; добавление - «забыть»'}
    return view(job)


RUN.mkdir(exist_ok=True)
path = RUN / 'manager.sock'
path.unlink(missing_ok=True)
with socket.socket(socket.AF_UNIX) as listener:
    listener.bind(str(path))
    listener.listen(16)
    while True:
        connection, _ = listener.accept()
        with connection:
            try:
                method, params = validate(json.loads(connection.makefile('rb').readline()))
                reply = {'api': 1, 'ok': True, 'result': execute(method, params)}
            except (Invalid, ValueError):
                reply = {'api': 1, 'ok': False, 'error': 'validation'}
            connection.sendall(json.dumps(reply, ensure_ascii=False).encode() + b'\n')
