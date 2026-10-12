#!/usr/bin/env python3
"""YouTube: три провала видеопотока → VPN, три успеха → zapret.

Вызывается systemd-таймером; отдельная служба не задерживает быстрый сторож
экзитов. Обход проверяется напрямую даже во время работы через VPN.
4 МиБ / минимум 250000 байт/с (2 Мбит/с): отсеиваем сильное замедление,
не обещая HD/4K. До 5.7 ГиБ/сутки при минутном периоде; фактически меньше,
поскольку таймер отсчитывает минуту от окончания предыдущей проверки.
YT_PROBE_VIDEO позволяет выбрать другой общедоступный длинный ролик.
--probe-only проверяет поток без изменения маршрута или состояния.
--probe-quic отдельно измеряет QUIC (нужен curl с HTTP3); результат не меняет
selector: при allow владелец допускает задержку до отката плеера на TCP.
--status читает текущий selector и результат последней пробы без изменений.
"""
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import urllib.parse
import urllib.request

STATE = Path('/var/lib/anysda-yt-failover/state.json')
CONFIG = Path('/etc/sing-box/config.json')
BOT_SECRET = Path('/etc/anysda/tgbot-secret.txt')
VIDEO = os.environ.get('YT_PROBE_VIDEO', 'https://www.youtube.com/watch?v=aqz-KE-bpKQ')
SAMPLE_BYTES = 4 * 1024 * 1024
MIN_BPS = 250000
HITS = 3


def request(url, method, body, headers):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    # Локальные управляющие запросы никогда не отправляем через HTTP_PROXY.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def api(method, body=None):
    cfg = json.loads(CONFIG.read_text())
    clash = cfg['experimental']['clash_api']
    return request('http://' + clash['external_controller'] + '/proxies/youtube-auto', method, body,
                   {'Authorization': 'Bearer ' + clash['secret'], 'Content-Type': 'application/json'})


def notify(detail):
    if not BOT_SECRET.exists():
        # Бот необязателен; событие уже записано в journald.
        return True
    try:
        request('http://127.0.0.1:8877/event', 'POST',
                {'type': 'system_alert', 'subject': 'Маршрут YouTube', 'detail': detail},
                {'X-Tgbot-Secret': BOT_SECRET.read_text().strip(), 'Content-Type': 'application/json'})
        return True
    except (OSError, ValueError):
        print('YouTube: алерт не доставлен, повтор при следующей проверке', flush=True)
        return False


def media_ok(returncode, stats):
    return (returncode == 0 and stats.get('http_code') == 206
            and stats.get('size_download', 0) == SAMPLE_BYTES
            and stats.get('speed_download', 0) >= MIN_BPS
            and str(stats.get('content_type', '')).startswith('video/'))


def probe(quic=False):
    try:
        # Службы, очередь и счётчики: ответ CDN без живого десинка не успех.
        ready = subprocess.run(['/usr/local/bin/anysda-yt-check'],
                               capture_output=True, timeout=35, check=False)
        if ready.returncode:
            return False, 'очередь или TCP-путь обхода не прошли A/B-проверку'
        user = pwd.getpwnam('ytzdiag')
        prefix = ['setpriv', f'--reuid={user.pw_uid}', f'--regid={user.pw_gid}', '--clear-groups']
        info = subprocess.run(prefix + [
            '/opt/anysda-yt-probe/bin/yt-dlp', '--ignore-config', '--no-cache-dir',
            '--no-playlist', '--no-warnings', '--proxy', '', '--force-ipv4',
            '--js-runtimes', 'node:/opt/anysda-yt-probe/bin/node',
            '--socket-timeout', '10', '--retries', '0',
            '--extractor-retries', '0', '-f', 'bestvideo[protocol=https]', '-J', VIDEO,
        ], capture_output=True, text=True, timeout=60, check=False)
        if info.returncode:
            if 'Sign in to confirm' in (info.stderr or ''):
                return False, 'YouTube требует входа; обход не измерен'
            return False, 'не удалось получить URL видеопотока (yt-dlp); проверить ролик/экстрактор'
        media = json.loads(info.stdout)
        url = media['url']
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.googlevideo.com'):
            return False, 'экстрактор не вернул HTTPS-видеопоток Googlevideo'
        # Range ограничивает расход трафика. --max-filesize также защищает от
        # CDN, игнорирующего Range. Ни HTTP 200, ни оборванная передача не OK.
        cmd = prefix + ['curl', '-q', '-4', '--noproxy', '*', '--http3-only' if quic else '--http1.1',
                        '--silent', '--show-error', '--fail', '--connect-timeout', '5',
                        '--max-time', '20', '--range', f'0-{SAMPLE_BYTES - 1}',
                        '--max-filesize', str(SAMPLE_BYTES), '-o', '/dev/null', '-w', '%{json}']
        for key, value in media.get('http_headers', {}).items():
            if key.lower() in ('user-agent', 'referer', 'origin'):
                cmd += ['-H', f'{key}: {value}']
        result = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=25, check=False)
        stats = json.loads(result.stdout or '{}')
        good = media_ok(result.returncode, stats)
        transport = 'QUIC' if quic else 'TCP'
        return good, (f'{transport} video: HTTP {stats.get("http_code", 0)}, '
                      f'{stats.get("size_download", 0)} байт, '
                      f'{stats.get("speed_download", 0)} байт/с, curl={result.returncode}')
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        # stdout yt-dlp и подписанные URL не пишем в журнал.
        return False, f'видеопроба не завершена: {type(error).__name__}'


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False) + '\n')
    temporary.replace(path)


def tick(path, sample):
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict) or not isinstance(state.get('pending'), list):
            raise ValueError('state')
        for key in ('good', 'bad'):
            if type(state.get(key)) is not int or not 0 <= state[key] <= HITS:
                raise ValueError('counter')
    except (FileNotFoundError, ValueError):
        state = {'good': 0, 'bad': 0, 'pending': []}
    current = api('GET')['now']
    good, detail = sample()
    state['good'] = min(HITS, state['good'] + 1) if good else 0
    state['bad'] = min(HITS, state['bad'] + 1) if not good else 0
    state['last_probe'] = detail
    save(path, state)
    target = ('youtube-ru' if state['good'] >= HITS else
              'foreign-best' if state['bad'] >= HITS else current)
    if target != current:
        api('PUT', {'name': target})
        if api('GET')['now'] != target:
            raise OSError('selector не подтвердил смену маршрута')
        event = f'{current} → {target}; {detail}'
        print('YouTube: ' + event, flush=True)
        state['pending'].append(event)
        save(path, state)
    state['pending'] = [event for event in state['pending'] if not notify(event)]
    save(path, state)
    print(f'YouTube: {target}; good={state["good"]} bad={state["bad"]}; {detail}', flush=True)


def main():
    if sys.argv[1:] in (['--probe-only'], ['--probe-quic']):
        good, detail = probe(quic=sys.argv[1] == '--probe-quic')
        print(detail)
        return 0 if good else 1
    cfg = json.loads(CONFIG.read_text())
    if not any(o.get('tag') == 'youtube-auto' for o in cfg.get('outbounds', [])):
        # Стадия 19 предшествует 20: старый роутер пока без selector.
        print('YouTube: ожидаю конфиг youtube-auto от стадии 20', flush=True)
        return 0
    if sys.argv[1:] == ['--status']:
        print('YouTube selector: ' + api('GET')['now'])
        if STATE.exists():
            state = json.loads(STATE.read_text())
            print(f'good={state["good"]} bad={state["bad"]}; {state.get("last_probe", "нет измерений")}')
        return 0
    tick(STATE, probe)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as failure:
        print(f'YouTube: ошибка контроллера {type(failure).__name__}', file=sys.stderr)
        sys.exit(1)
