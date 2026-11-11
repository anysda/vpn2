"""Шаг стадии 25: VictoriaMetrics на entry.

Скрейп node_exporter: entry по loopback, экзиты через служебный Hysteria2-туннель -
у каждого свой SOCKS-вход sing-box (mon-<тег>), порт выводится из последнего октета
служебного адреса той же формулой, что в vpn2.router.config.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from vpn2 import fsutil
from vpn2.containers import compose, engine

STAGE = '25-monitoring'
SCRAPE = '/etc/anysda/vmsingle-scrape.yml'
TARGETS = 'http://127.0.0.1:8428/api/v1/targets'
REPOSITORY = 'victoriametrics/victoria-metrics'


def targets(url=TARGETS):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def healthy(reply, expected):
    """Успешный скрейп entry и каждого настроенного экзита: HTTP 200 и health=down не годятся."""
    active = (reply.get('data') or {}).get('activeTargets') or []
    return (reply.get('status') == 'success' and len(active) == expected
            and all(target.get('health') == 'up' for target in active))


def run(environ, *, docker=None, say=engine.say, probe=targets, sleep=None, scrape_path=SCRAPE):
    docker = docker or engine.Docker()
    tags = compose.exit_tags(environ)
    if not tags:
        raise engine.EngineError('EXIT_TAGS пуст')
    text, skipped = compose.scrape_config(environ)
    for tag in skipped:
        say(f'ВНИМАНИЕ: MGMT_IP_{compose.upper(tag)} не задан — {tag} пропущен в scrape config')
    path = Path(scrape_path)
    os.makedirs(path.parent, exist_ok=True)
    os.makedirs('/var/lib/vmsingle', exist_ok=True)
    changed = not path.is_file() or path.read_text() != text
    if changed:
        fsutil.atomic(path, text, mode=0o644, encoding='utf-8')
    engine.drop_timezone_dir()
    image = f'{REPOSITORY}:{environ["VM_VERSION"]}'
    service = compose.vm_service(image, engine.resource_text(environ, 'vm'), text)
    changed |= docker.publish(compose.VM, service)
    changed |= docker.up(compose.VM)
    expected = 1 + len(tags)
    reply = {}

    def ready():
        nonlocal reply
        reply = probe()
        return healthy(reply, expected)

    kwargs = {'sleep': sleep} if sleep else {}
    ok = engine.wait(ready, 30, 1, **kwargs)
    say('scrape health:')
    for target in (reply.get('data') or {}).get('activeTargets') or []:
        host = (target.get('labels') or {}).get('host') or '?'
        print(f'  {host} ({target.get("scrapeUrl", "")}): {target.get("health", "")}')
    if not ok:
        raise engine.EngineError(f'мониторинг не готов: ожидается {expected} доступных scrape-целей')
    engine.stamp(STAGE)
    return changed
