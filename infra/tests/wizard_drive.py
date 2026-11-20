"""Мастер установки на стенде без браузера: ссылка -> черновик -> установка -> итог -> ссылка мертва, мастер ушёл.

python3 wizard_drive.py <ссылка> <ключ ssh админа> <тег> <root@адрес экзита> <пароль панели> fingerprint=<из терминала>|ca=<корень ACME>
Связь пропадает (перезапуск Caddy на стадии 30, менеджера на 29-control) - опрос идёт дальше; обрыв дольше 10 с - провал.
"""
import hashlib
import http.client
import json
import socket
import ssl
import sys
import time
import urllib.parse

link, admin_key, tag, host, password, tls_check = sys.argv[1:]
url = urllib.parse.urlsplit(link)
ADDRESS, TOKEN = url.hostname, url.fragment
CONTEXT = ssl._create_unverified_context()
cookie = ''


def log(text):
    print(f'[wizard] {text}', flush=True)


def die(text):
    log(f'✗ {text}')
    sys.exit(1)


def call(method, path, body=None, auth=True):
    global cookie
    connection = http.client.HTTPSConnection(ADDRESS, 443, timeout=60, context=CONTEXT)
    headers = {'Content-Type': 'application/json', **({'Cookie': f'vpn2_wizard={cookie}'} if auth and cookie else {})}
    connection.request(method, path, None if body is None else json.dumps(body), headers)
    response = connection.getresponse()
    data = response.read().decode(errors='replace')
    if found := response.getheader('Set-Cookie'):
        cookie = found.split(';')[0].split('=', 1)[1]
    try:
        return response.status, json.loads(data)
    except ValueError:
        return response.status, data


def must(method, path, body=None):
    code, answer = call(method, path, body)
    if code != 200:
        die(f'{method} {path}: {code} {answer}')
    return answer


def served():
    with socket.create_connection((ADDRESS, 443), timeout=10) as sock, CONTEXT.wrap_socket(sock, server_hostname=ADDRESS) as wrapped:
        return ':'.join(f'{byte:02X}' for byte in hashlib.sha256(wrapped.getpeercert(True)).digest())


kind, _, value = tls_check.partition('=')
if kind == 'fingerprint':
    log(f'терминал: {value}, сайт: {served()}')
    if not value or value != served():
        die('отпечаток в терминале не тот, что отдаёт сайт мастера')
else:
    # Сертификат ACME: браузер открыл бы ссылку без предупреждения - проверка по корню и по IP.
    CONTEXT = ssl.create_default_context(cafile=value)
    log(f'сертификат ACME на {ADDRESS}: {served()}')
for path in ('/api/state', '/api/status', '/api/job'):
    if call('GET', path, auth=False) != (401, {'refused': 'Ссылка устарела - запустите ./deploy.sh ещё раз'}):
        die(f'{path} без сессии отдал не 401')
if call('POST', '/api/session', {'token': TOKEN[:-1] + ('A' if TOKEN[-1] != 'A' else 'B')})[0] != 401:
    die('чужой токен принят')
must('POST', '/api/session', {'token': TOKEN})
state = must('GET', '/api/state')
log(f'сессия: этап {state["phase"]}, ссылка до {time.strftime("%H:%M:%S", time.gmtime(state["link_expires_at"]))} UTC')
must('PUT', '/api/draft', {'admin_ssh_key': admin_key, 'exits': [{'tag': tag, 'host': host, 'skipped': False}],
                           'panel': {'domain': '', 'admin_user': 'anysda'}, 'step': 6})
problems = must('POST', '/api/exit/check', {'tag': tag})['problems']
if problems:
    die(f'проверка экзита: {problems}')
must('PUT', '/api/secret/admin_password', {'value': password})
key = must('POST', '/api/backup-key')
must('POST', '/api/backup-key/seen', {'recipient': key['recipient']})
job = must('POST', '/api/install')['job']
log(f'установка: задание {job["id"]}')
started, lost, worst, stage = time.monotonic(), None, 0.0, ''
while True:
    try:
        code, state = call('GET', '/api/state')
    except (OSError, http.client.HTTPException):
        code, state = None, None
    now = time.monotonic()
    if code != 200:
        lost = lost or now
        if now - lost > 10:
            die(f'мастер не отвечает дольше 10 с (ответ {code}) на {stage}')
        time.sleep(1)
        continue
    if lost:
        worst = max(worst, now - lost)
        log(f'связь восстановилась через {now - lost:.1f} с на {stage}')
        lost = None
    current = next((item['name'] for item in state['job']['stages'] if item['state'] == 'running'), stage)
    if current != stage:
        log(f'стадия {current} ({int(now - started)} с)')
        stage = current
    if state['phase'] == 'done':
        break
    if state['job']['state'] in ('failed', 'interrupted'):
        die(f'установка: {state["job"]["state"]}: {state["job"]["error"]}\n{state["job"]["log"][-3000:]}')
    if now - started > 1800:
        die('установка не кончилась за 30 мин')
    time.sleep(2)
log(f'установка кончилась за {int(time.monotonic() - started)} с, худший обрыв связи {worst:.1f} с')
status = must('GET', '/api/status')
for item in status['systems']:
    log(f'  {item["state"]:<8} {item["name"]}: {item["detail"]}')
if any(item['state'] == 'failed' for item in status['systems']) or status['panel_url'] != f'https://{ADDRESS}/':
    die(f'итог: {status}')
if call('POST', '/api/session', {'token': TOKEN}, auth=False)[0] != 401:
    die('ссылка жива после конца установки')
# «Открыть панель» с экрана итога - тот же адрес: мастер переадресует и уходит, следующий запрос - панель.
code, _ = call('GET', '/')
if code != 302:
    die(f'переход со страницы итога: {code}, не 302')
code, answer = call('GET', '/api/state', auth=False)
if code == 401 and 'refused' in answer:
    die('после перехода на панель отвечает мастер')
log(f'«Открыть панель»: 302, затем панель (/api/state -> {code})')
