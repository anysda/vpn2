"""YouTube: три провала видеопотока - через VPN, три успеха - через zapret.

Пробу запускает таймер anysda-yt-failover. Обход проверяется напрямую и во время
работы через VPN: 4 МиБ видеопотока не медленнее 250000 байт/с. Неизмеренная проба
(антибот, экстрактор, лимит памяти) счётчики и маршрут не меняет; «не измерено»
уходит в бот, когда замера нет дольше часа, «возобновлено» - после такого алерта.
"""
import json
import math
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from vpn2 import fsutil
from vpn2 import notify as bot
from vpn2 import probe as clash

STATE = Path('/var/lib/anysda-yt-failover/state.json')
CONFIG = Path('/etc/sing-box/config.json')
BOT_SECRET = Path('/etc/anysda/tgbot-secret.txt')
VIDEO = os.environ.get('YT_PROBE_VIDEO', 'https://www.youtube.com/watch?v=aqz-KE-bpKQ')
POT_VIDEO = os.environ.get('YT_PROBE_POT_VIDEO', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
SAMPLE_BYTES = 4 * 1024 * 1024
MIN_BPS = 250000
HITS = 3
UNMEASURED_ALERT_AFTER = 3600
PLAYER_CLIENT = 'web_embedded'
# Один на процесс: локальные запросы мимо HTTP_PROXY.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# Убийство по MemoryMax юнита и сетевой отказ - сбой самой пробы: второй заход
# (node + bgutil) их не лечит.
MEMORY_LIMIT = 'проба не удалась: лимит памяти'
OOM_MARKERS = ('out of memory', 'memoryerror', 'cannot allocate memory', 'sigkill')
NETWORK_FAILURE = 'сетевой отказ до YouTube'
NETWORK_MARKERS = ('timed out', 'connection refused', 'connection reset', 'network is unreachable',
                   'no route to host', 'name or service not known',
                   'temporary failure in name resolution', 'no address associated with hostname')
YT_DLP = '/opt/anysda-yt-probe/bin/yt-dlp'


def api(method, body=None):
    client = clash.ClashApi.from_config(CONFIG, opener=OPENER)
    if method == 'PUT':
        client.select('youtube-auto', body['name'], timeout=5)
        return {}
    return client.proxy('youtube-auto', timeout=5)


def notify(detail):
    event = {'type': 'system_alert', 'subject': 'Маршрут YouTube', 'detail': detail}
    if bot.bot_event(event, secret_file=BOT_SECRET, confirm=False, opener=OPENER) is bot.Outcome.FAILED:
        print('YouTube: алерт не доставлен, повтор при следующей проверке', flush=True)
        return False
    return True


def media_ok(returncode, stats):
    """True/False - измеренная загрузка; None - недостаточно сетевых данных."""
    if not isinstance(stats, dict):
        return None
    code = stats.get('http_code')
    # 403/429/5xx, редирект и ответ без Range (200) не измеряют обход.
    if type(code) is not int or code not in (0, 206):
        return None
    if code == 206 and not str(stats.get('content_type', '')).startswith('video/'):
        return None
    # URL Googlevideo уже получен: сброс TLS (35), таймаут (28) и обрыв (18/56) -
    # отказ пути. DNS (6), сертификат (60) и локальные ошибки curl не измерены.
    if returncode in (7, 18, 28, 35, 52, 55, 56):
        return False
    if returncode != 0 or code != 206:
        return None
    size, speed = stats.get('size_download'), stats.get('speed_download')
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
           for value in (size, speed)) or size > SAMPLE_BYTES:
        return None
    return size == SAMPLE_BYTES and speed >= MIN_BPS


def oom_kills():
    """Счётчик OOM-kill cgroup службы; ловит и node, убитый внутри yt-dlp."""
    try:
        group = Path('/proc/self/cgroup').read_text().split('::', 1)[1].strip()
        for line in Path('/sys/fs/cgroup' + group, 'memory.events').read_text().splitlines():
            key, value = line.split()
            if key == 'oom_kill':
                return int(value)
    except (OSError, ValueError, IndexError):
        pass
    return None


def memory_killed(result, before):
    if result.returncode in (-9, 137) or any(m in (result.stderr or '').lower() for m in OOM_MARKERS):
        return True
    after = oom_kills() if before is not None else None
    return after is not None and after > before


def _googlevideo(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed if parsed.scheme == 'https' and (parsed.hostname or '').endswith('.googlevideo.com') else None


def _extract(prefix, env, video, client, cache):
    """(media, None) - JSON yt-dlp с URL видеопотока, иначе (None, причина)."""
    extra = []
    if cache is not None:
        extra = ['--source-address', '0.0.0.0', '--extractor-args',
                 'youtubepot-bgutilscript:server_home=/opt/anysda-yt-probe/bgutil/server']
    before = oom_kills()
    info = subprocess.run(prefix + [
        YT_DLP, '--ignore-config', '--no-cache-dir', '--no-playlist', '--no-warnings', '--proxy', '',
        '--force-ipv4', '--js-runtimes', 'node:/opt/anysda-yt-probe/bin/node',
        '--extractor-args', f'youtube:player_client={client}' + (';fetch_pot=always' if cache else ''),
        '--socket-timeout', '10', '--retries', '0', '--extractor-retries', '0',
        '-f', 'bestvideo[protocol=https]', '-J', video,
    ] + extra, env=env, capture_output=True, text=True, timeout=60, check=False)
    if memory_killed(info, before):
        return None, f'{MEMORY_LIMIT} (yt-dlp/node, client={client})'
    if info.returncode:
        stderr = info.stderr or ''
        if 'Sign in to confirm' in stderr:
            return None, 'YouTube требует входа (антибот); обход не измерен'
        if 'Requested format is not available' in stderr:
            return None, f'нет HTTPS-видеоформата (SABR/ограничения ролика), client={client}'
        # HTTP-ответ (403 и т.п.) - YouTube ответил: это не сеть.
        if 'HTTP Error' not in stderr and any(marker in stderr.lower() for marker in NETWORK_MARKERS):
            return None, f'{NETWORK_FAILURE} (таймаут, соединение или DNS у yt-dlp), client={client}'
        return None, (f'не удалось получить URL видеопотока (yt-dlp, client={client}); '
                      'проверить встраивание ролика/экстрактор/JS-рантайм')
    media = json.loads(info.stdout)
    if not isinstance(media, dict) or not isinstance(media.get('url'), str):
        return None, 'экстрактор не вернул URL видеопотока'
    parsed = _googlevideo(media['url'])
    if not parsed:
        return None, 'экстрактор не вернул HTTPS-видеопоток Googlevideo'
    if cache is not None and not urllib.parse.parse_qs(parsed.query).get('pot'):
        return None, f'локальный PO-токен не получен, client={client}'
    return media, None


def probe_media(user, video, client, cache=None):
    prefix = ['setpriv', f'--reuid={user.pw_uid}', f'--regid={user.pw_gid}', '--clear-groups']
    # node провайдера PO-токена тоже идёт напрямую, с тем же IPv4 и UID.
    env = {k: v for k, v in os.environ.items() if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
    if cache is not None:
        env['XDG_CACHE_HOME'] = cache
    try:
        media, problem = _extract(prefix, env, video, client, cache)
        if problem:
            return None, problem
        # Range ограничивает трафик, --max-filesize - на CDN без Range; ни 200, ни обрыв не OK.
        cmd = prefix + ['curl', '-q', '-4', '--noproxy', '*', '--http1.1', '--silent', '--show-error', '--fail',
                        '--connect-timeout', '5', '--location', '--max-redirs', '3', '--proto-redir', '=https',
                        '--max-time', '20', '--range', f'0-{SAMPLE_BYTES - 1}',
                        '--max-filesize', str(SAMPLE_BYTES), '-o', '/dev/null', '-w', '%{json}']
        for key, value in (media.get('http_headers') or {}).items():
            if key.lower() in ('user-agent', 'referer', 'origin'):
                cmd += ['-H', f'{key}: {value}']
        before = oom_kills()
        result = subprocess.run(cmd + [media['url']], env=env, capture_output=True, text=True, timeout=25, check=False)
        if memory_killed(result, before):
            return None, f'{MEMORY_LIMIT} (curl, client={client})'
        if result.returncode == 6:
            return None, f'{NETWORK_FAILURE}: DNS Googlevideo (curl=6), client={client}'
        stats = json.loads(result.stdout or '{}')
        if not isinstance(stats, dict):
            return None, 'curl не вернул объект статистики видеопотока'
        effective = _googlevideo(str(stats.get('url_effective', media['url'])))
        if not effective:
            return None, 'CDN перенаправил запрос за пределы HTTPS Googlevideo'
        return media_ok(result.returncode, stats), (
            f'TCP video: HTTP {stats.get("http_code", 0)}, {stats.get("size_download", 0)} байт, '
            f'{stats.get("speed_download", 0)} байт/с, curl={result.returncode}, '
            f'CDN={effective.hostname}, client={client}' + (', PO=local' if cache else ''))
    except subprocess.TimeoutExpired as error:
        # Повисла утилита или путь: второй заход bgutil на том же пути повиснет так же.
        return None, f'{NETWORK_FAILURE}: видеопроба не завершена: {type(error).__name__}'
    except (OSError, ValueError, KeyError, AttributeError) as error:
        # stdout yt-dlp и подписанные URL в журнал не идут.
        return None, f'видеопроба не завершена: {type(error).__name__}'


def probe():
    try:
        # Ответ CDN без живой очереди десинка не считается успехом.
        ready = subprocess.run(['/usr/local/bin/anysda-yt-check'], capture_output=True, timeout=35, check=False)
        if ready.returncode == 1:
            return False, 'очередь, nft-правила или TCP-путь обхода не работают'
        if ready.returncode:
            return None, 'A/B-проверка не завершена; проверить утилиты диагностики'
        user = pwd.getpwnam('ytzdiag')
        good, detail = probe_media(user, VIDEO, PLAYER_CLIENT)
        # Измеренный ответ, лимит памяти и сеть второй заход не перепроверяет.
        if good is not None or detail.startswith((MEMORY_LIMIT, NETWORK_FAILURE)):
            return good, detail
        with tempfile.TemporaryDirectory(prefix='anysda-yt-pot-') as cache:
            os.chown(cache, user.pw_uid, user.pw_gid)
            fallback, fallback_detail = probe_media(user, POT_VIDEO, 'mweb', cache)
        return fallback, f'{fallback_detail}; первая проба: {detail}'
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        return None, f'видеопроба не завершена: {type(error).__name__}'


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic(path, json.dumps(state, ensure_ascii=False) + '\n', mode=0o600)


def tick(path, sample):
    try:
        state = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        state = {'good': 0, 'bad': 0, 'pending': []}
    current = api('GET')['now']
    good, detail = sample()
    unmeasured, now = good is None, time.time()
    since = state.get('unmeasured_since') if state.get('unmeasured') is True else None
    alerted = state.get('unmeasured_alerted') is True
    event = None
    if not unmeasured:
        if alerted:
            event = f'измерение возобновлено: {detail}. Автопереключение снова активно.'
        since, alerted = None, False
    elif type(since) not in (int, float) or not 0 < since <= now:
        since = now
    elif not alerted and now - since > UNMEASURED_ALERT_AFTER:
        event = f'не измерено: {detail}. Счётчики и маршрут сохранены ({current}).'
        alerted = True
    if event:
        print('YouTube: ' + event, flush=True)
        state['pending'].append(event)
    state.update(unmeasured=unmeasured, unmeasured_since=since, unmeasured_alerted=alerted,
                 last_probe=detail, checked_at=now)
    if not unmeasured:
        state['good'] = min(HITS, state['good'] + 1) if good else 0
        state['bad'] = min(HITS, state['bad'] + 1) if not good else 0
    save(path, state)
    target = (current if unmeasured else 'youtube-ru' if state['good'] >= HITS
              else 'foreign-best' if state['bad'] >= HITS else current)
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
    if detail.startswith(MEMORY_LIMIT):
        print(f'YouTube: {detail}; не считается провалом YouTube, счётчики и маршрут {target} сохранены',
              flush=True)
        return
    print(f'YouTube: {target}; good={state["good"]} bad={state["bad"]}; '
          f'{"не измерено: " if unmeasured else ""}{detail}', flush=True)


def run(argv=None):
    """vpn2 watchdog yt: сбой контроллера - строка в stderr и код 1."""
    del argv
    try:
        config = json.loads(CONFIG.read_text())
        if not any(o.get('tag') == 'youtube-auto' for o in config.get('outbounds', [])):
            # Стадия 19 идёт раньше 20: роутер ещё без selector.
            print('YouTube: ожидаю конфиг youtube-auto от стадии 20', flush=True)
            return 0
        tick(STATE, probe)
    except (OSError, ValueError, KeyError) as failure:
        print(f'YouTube: ошибка контроллера {type(failure).__name__}', file=sys.stderr)
        return 1
    return 0
