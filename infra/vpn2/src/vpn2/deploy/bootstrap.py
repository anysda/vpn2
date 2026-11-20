"""./deploy.sh на чистой системе: мастер установки в браузере вместо config.yaml.

Под замком деплоя, системным python3 из дерева: Caddy и сертификат на IP entry (Let's Encrypt, не ответил за минуту -
самоподписанный), пока ставится Docker и из образа выпуска берётся страница мастера; затем ключ оркестратора,
службы менеджера установки и мастера, ссылка с токеном на час. Установку ведёт менеджер, этот процесс отдаёт
терминал. Идёт задание (замок занят) или мастер уже стоит - только новая ссылка: прежняя и её сессии мертвы.
"""
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from vpn2 import fsutil, tls, wizard
from vpn2.deploy import G, Y, E, INFRA, REPO, die, docker_install, orchestrator_key, ok, step, svc

UNITS = Path(os.environ.get('VPN2_UNIT_DIR') or '/etc/systemd/system')
# Стадии обновляют openssh с NEEDRESTART_MODE=a: needrestart перезапустил бы менеджер и задание установки
# (python системы обновлён тем же apt), задание стало бы прерванным.
NEEDRESTART = Path('/etc/needrestart/conf.d')
UNIT = '''[Unit]
Description={description}
After=network-online.target

[Service]
Environment=DEPLOY_ROOT={infra} PYTHONPATH={infra}/vpn2/src
{extra}ExecStart=/usr/bin/python3 -B -m {module} serve
UMask=0077
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
'''


def entry_ip():
    """Адрес entry в ссылке: источник маршрута в интернет."""
    found = subprocess.run(['ip', '-4', 'route', 'get', '1.1.1.1'], capture_output=True, text=True).stdout.split()
    return found[found.index('src') + 1] if 'src' in found else die('не найден IPv4-адрес entry (ip route get 1.1.1.1)')


def firewall():
    """ufw уже включён (не наш образ): 80 и 443 - с теми же комментариями, что у стадий."""
    if 'Status: active' not in subprocess.run(['ufw', 'status'], capture_output=True, text=True).stdout:
        return
    from vpn2 import firewall as fw
    fw.ufw_reconcile('caddy HTTP + ACME challenge', ['80/tcp'], ['caddy HTTP панель'])
    fw.ufw_reconcile('caddy HTTPS панель', ['443/tcp'])


def certificate(ip):
    """Caddy, сайт панели на IP с сертификатом; копия пары IP - мастеру: с доменом стадия 30 сменит пару сайта."""
    tls.install_caddy()
    fingerprint = tls.ip_certificate(ip, acme_url=os.environ.get('TLS_ACME_URL') or tls.LE,
                                     acme_root=os.environ.get('TLS_ACME_ROOT') or '')
    cert, key = wizard.CADDY / tls.SITE_CERT, wizard.CADDY / tls.SITE_KEY
    # Повтор после стадии 30 с доменом: у сайта уже пара домена, копия IP остаётся прежней.
    with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
        if tls.certificate(cert, key, ip, time.time()):
            shutil.copy(cert, wizard.CADDY / 'vpn2-wizard.crt')
            shutil.copy(key, wizard.CADDY / 'vpn2-wizard.key')
            if os.geteuid() == 0:
                shutil.chown(wizard.CADDY / 'vpn2-wizard.key', group='caddy')
    routes, own = wizard.site(ip)
    tls.serve_routes('wizard', routes, site=own)
    return fingerprint


def page(environ):
    """Страница мастера из образа выпуска (как статика аварийки): docker create, cp, контейнер убирается."""
    from vpn2 import release
    from vpn2.containers import compose
    if environ.get('VPN2_DEVELOPMENT') != 'true' and not environ.get('VPN2_RELEASE_MANIFEST'):
        environ['VPN2_RELEASE_MANIFEST'] = str(compose.MANIFEST)
        if release.main(['--manifest', str(compose.MANIFEST), 'fetch', str(REPO)]):
            raise SystemExit(1)
    try:
        image = compose.release_image(environ, 'wizard', compose.Docker())
    except compose.EngineError:
        # Разработка без образа мастера: страница, что уже лежит на узле.
        if environ.get('VPN2_DEVELOPMENT') != 'true' or not (wizard.STATIC / 'index.html').is_file():
            raise
        print(f'WARNING: [development] wizard: образа нет, страница из {wizard.STATIC}', file=sys.stderr)
        return
    home = wizard.STATIC.parent
    home.mkdir(parents=True, exist_ok=True)
    subprocess.run(['docker', 'rm', '-f', 'vpn2-wizard'], capture_output=True)
    subprocess.run(['docker', 'create', '--name', 'vpn2-wizard', '--network', 'none', image], check=True, capture_output=True)
    try:
        with tempfile.TemporaryDirectory(prefix='.staging.', dir=home) as staging:
            subprocess.run(['docker', 'cp', 'vpn2-wizard:/opt/vpn2-wizard/static/.', f'{staging}/static/'], check=True,
                           capture_output=True)
            if not Path(staging, 'static/index.html').is_file():
                die(f'в образе мастера {image} нет страницы (index.html)')
            shutil.rmtree(wizard.STATIC, ignore_errors=True)
            os.replace(Path(staging, 'static'), wizard.STATIC)
    finally:
        subprocess.run(['docker', 'rm', '-f', 'vpn2-wizard'], capture_output=True)


def units(environ):
    """Менеджер установки из дерева (его службу 29-control заменит своей из venv - она не трогается) и мастер."""
    from vpn2.nodevars import RUN_NAMES
    # Настройки запуска мимо config.yaml (--development, тестовый ACME стенда) - менеджеру и его заданиям.
    extra = ''.join(f'Environment="{name}={environ[name]}"\n' for name in sorted(RUN_NAMES) if environ.get(name))
    texts = {'vpn2-manager': ('VPN2 install manager (before install: from the tree)', 'vpn2.manager'),
             wizard.UNIT: ('VPN2 install wizard in the browser', 'vpn2.wizard')}
    for name, (description, module) in texts.items():
        path = UNITS / f'{name}.service'
        if name == 'vpn2-manager' and path.exists():
            continue
        fsutil.atomic(path, UNIT.format(description=description, infra=INFRA, extra=extra, module=module), 0o644)
    if NEEDRESTART.is_dir():
        fsutil.atomic(NEEDRESTART / 'vpn2.conf', '$nrconf{override_rc}{qr(^vpn2-(manager|wizard))} = 0;\n', 0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'vpn2-manager.service', f'{wizard.UNIT}.service'], check=True,
                   capture_output=True)
    subprocess.run(['systemctl', 'restart', f'{wizard.UNIT}.service'], check=True)


def setup(environ):
    t0 = time.monotonic()
    ip = entry_ip()
    step(f'мастер установки: entry {ip}')
    docker = docker_install()
    result = {}

    def tls_part():
        try:
            firewall()
            result['fingerprint'] = certificate(ip)
        except Exception as error:  # noqa: BLE001 - причина уходит в терминал ниже
            result['error'] = error
    # Ctrl-C во время ожидания ACME (до минуты) - выход сразу: замок деплоя не держит поток сертификата.
    worker = threading.Thread(target=tls_part, daemon=True)
    worker.start()
    code = svc('apt_have age || { apt_update_if_stale; NEEDRESTART_SUSPEND=1 apt_get install -y -qq age >/dev/null; }')
    if docker and docker.wait():
        die('Docker на entry не установился, причина выше')
    if code:
        die('age (ключ бэкапов) не установился, причина выше')
    page(environ)
    worker.join()
    if 'error' in result:
        die(f'HTTPS мастера не поднялся: {result["error"]}')
    orchestrator_key(create=True)
    wizard.STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    fsutil.write_json(wizard.STATE / 'entry.json', {'host': ip})
    if not (wizard.STATE / 'draft.json').exists():
        fsutil.write_json(wizard.STATE / 'draft.json', wizard.DRAFT)
    units(environ)
    ok(f'мастер установки готов за {int(time.monotonic() - t0)} с')
    return ip, result['fingerprint']


def link(ip, fingerprint):
    token = wizard.new_link()
    print(f'\n  Откройте в браузере:  {G}https://{ip}/#{token}{E}\n', flush=True)
    if fingerprint:
        print(f'  {Y}{tls.notice({"self_signed": fingerprint})}{E}\n'
              '  браузер предупредит - сверьте отпечаток\n', flush=True)
    print('  Дальше в браузере; ссылка действует час, новая - ./deploy.sh\n', flush=True)


def main(argv):
    environ = os.environ
    if argv[:1] == ['--development']:
        environ['VPN2_DEVELOPMENT'] = 'true'
    elif argv[:1] == ['--manifest']:
        environ['VPN2_RELEASE_MANIFEST'] = str(Path(argv[1]).resolve())
    from vpn2 import manager
    try:
        with fsutil.lock(manager.LOCK, blocking=False):
            ip, fingerprint = setup(environ)
    except fsutil.LockBusy:
        # Идёт установка: мастер уже стоит, нужна только новая ссылка.
        if not (wizard.STATE / 'entry.json').is_file():
            die('deploy уже выполняется на этой машине')
        ip = wizard.read_json(wizard.STATE / 'entry.json', {})['host']
        fingerprint = tls.read_json('/etc/vpn2/tls/state.json', {}).get('self_signed', '')
    link(ip, fingerprint)
    return 0


def flags(argv):
    """Аргументы без флагов режима: --development, --manifest <файл>."""
    if argv[:1] == ['--development']:
        return argv[1:]
    return argv[2:] if argv[:1] == ['--manifest'] else argv


if __name__ == '__main__':
    if sys.argv[1:2] == ['--wanted']:
        sys.exit(0 if wizard.wanted(flags(sys.argv[2:])) else 1)
    sys.exit(main(sys.argv[1:]))
