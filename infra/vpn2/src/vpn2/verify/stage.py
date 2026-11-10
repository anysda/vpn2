"""Проверка узла после деплоя: 0 успешно, 1 отказ, 2 не проверено.

Строки вывода `[узел] ✓/✗/? проверка` читает deploy и стенд; отчёт для CI
собирается в VERIFY_REPORT, секретов в нём нет.
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from vpn2.verify import emergency, host
from vpn2.verify.exitssh import exit_ssh

REPORT = '/var/lib/anysda/verify-report.json'
TRAFFIC = Path(__file__).with_name('traffic.py')
STAMPS = Path('/var/anysda/.stamps')
EMERGENCY_ROOT = Path('/opt/vpn2-emergency')
EMERGENCY_STATE = Path('/var/lib/vpn2-emergency')
EMERGENCY_HY2 = Path('/etc/vpn2/emergency-hy2.json')
TLS_CONFIG = Path('/etc/vpn2/tls/config.json')
TLS_CERT = '/etc/vpn2/tls/ip/fullchain.pem'
NETWORKD_DROPIN = Path('/etc/systemd/networkd.conf.d/60-anysda-foreign.conf')
IKEV2_STATUS = Path('/etc/anysda/ikev2-status.json')
YT_CHECK = '/usr/local/bin/anysda-yt-check'
YT_FAILOVER = ('/opt/vpn2/venv/bin/vpn2', 'watchdog', 'yt')
TRAFFIC_LABEL = 'трафик: контрольный клиент'


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def say(text, stream=None):
    print(text, file=stream or sys.stdout, flush=True)


def ikev2_status():
    """Ошибка чтения, JSON и время из будущего не должны давать свежий статус."""
    try:
        age = time.time() - json.loads(IKEV2_STATUS.read_text())['updated']
    except emergency.BROKEN as error:
        return 1, emergency.problem(error)
    return (0 if 0 <= age < 120 else 1), f'ikev2 status age: {age:.0f}s'


class Stage:
    """Ход проверки одного узла: счётчики, строки отчёта, предупреждения."""

    def __init__(self, env, rows):
        self.env = env
        self.tag = env['HOST_TAG']
        self.exits = env.get('EXIT_TAGS', '').split()
        self.rows = Path(rows)
        self.warnings = []
        self.failures = self.unmeasured = 0
        self.image = False

    def get(self, name, default):
        return self.env.get(name) or default

    def line(self, text, stream=None):
        say(f'[{self.tag}] {text}', stream)

    def record(self, label, result, reason):
        component, _, name = label.partition(': ')
        if not name:
            component, name = self.tag, label
        row = {'check': name, 'component': component, 'result': result, 'reason': reason, 'time': now()}
        with self.rows.open('a', encoding='utf-8') as out:
            out.write(json.dumps(row, ensure_ascii=False) + '\n')

    def fail(self, label, reason='проверка не прошла'):
        self.line(f'✗ {label}', sys.stderr)
        self.record(label, 'Ошибка', reason)
        self.failures += 1

    def check(self, label, probe, *args, allow_unmeasured=False):
        rc, output = probe(*args)
        if output:
            say(output)
        last = output.rpartition('\n')[2]
        if rc == 0:
            self.line(f'✓ {label}')
            self.record(label, 'Работает', 'проверка прошла')
        elif rc == 2 and allow_unmeasured:
            last = last or 'команда завершилась с кодом 2'
            self.line(f'? {label} — не проверено: {last}')
            self.record(label, 'Не проверено', last)
            self.unmeasured += 1
        elif rc == 2:
            self.fail(label, 'код 2')
        else:
            self.fail(label, f'код {rc}' + (f': {last}' if output else ''))

    def warning(self, text):
        """Предупреждение не меняет код, но идёт в итог и в отчёт при любом исходе."""
        self.line(f'⚠ {text}', sys.stderr)
        self.warnings.append(text)

    def insecure_exits(self):
        """exits[].hy2_insecure: true - туннель до экзита без проверки сертификата."""
        for tag in self.exits:
            if self.env.get(tag.upper().replace('-', '_') + '_HY2_INSECURE') == '1':
                self.warning(f'{tag}: exits[].hy2_insecure: true — сертификат экзита не проверяется, '
                             'подмену экзита на пути entry → экзит не видно')

    def traffic(self):
        """Настоящий трафик: временный клиент WireGuard/OpenVPN/IKEv2 в своём netns.

        Строки отчёта модуль печатает и дописывает сам; код 0/1/2 как у стадии.
        """
        module = Path(self.get('VERIFY_TRAFFIC', TRAFFIC))
        if not module.is_file():
            self.fail(TRAFFIC_LABEL, f'модуль {module.name} не найден')
            return
        self.line('трафик: контрольный клиент в netns (до нескольких минут)')
        before = len(self.read_rows())
        rc = host.code(subprocess.run(
            [sys.executable, '-I', str(module), 'check', '--tag', self.tag, '--exits', ' '.join(self.exits),
             '--public-host', self.env.get('ENTRY_HOST', ''), '--report', str(self.rows)]).returncode)
        # Итог считает строки модуля, а не один его код: три «Ошибка» - три провала.
        fresh = [row['result'] for row in self.read_rows()[before:]]
        failed, unmeasured = fresh.count('Ошибка'), fresh.count('Не проверено')
        if rc == 2:
            self.unmeasured += unmeasured or 1
        elif rc:
            self.failures += failed or 1
            self.unmeasured += unmeasured
            # Модуль упал до своих строк: без строки отчёт выглядел бы чистым.
            if not fresh:
                self.line(f'✗ {TRAFFIC_LABEL} — код {rc}')
                self.record(TRAFFIC_LABEL, 'Ошибка', f'код {rc}')

    def read_rows(self):
        if not self.rows.exists():
            return []
        return [json.loads(line) for line in self.rows.read_text(encoding='utf-8').splitlines() if line.strip()]

    def write_report(self):
        path = Path(self.get('VERIFY_REPORT', REPORT))
        try:
            rows = self.read_rows()
            report = {'host': self.tag, 'code': self.code(), 'checks': rows, 'warnings': self.warnings, 'time': now()}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(f'{path}.tmp')
            tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(tmp, path)
        except (OSError, ValueError, KeyError, TypeError) as error:
            say(emergency.problem(error), sys.stderr)
            self.line(f'✗ отчёт: не записан {path}', sys.stderr)
            self.failures += 1
            return
        counts = {}
        for row in rows:
            counts[row['result']] = counts.get(row['result'], 0) + 1
        self.line(f'отчёт: {path} — ' + ', '.join(f'{name}: {count}' for name, count in sorted(counts.items())))

    def code(self):
        return 1 if self.failures else 2 if self.unmeasured else 0

    def finish(self):
        if self.failures:
            self.line(f'verify FAILED: проверок не пройдено: {self.failures}; не проверено: {self.unmeasured}',
                      sys.stderr)
        elif self.unmeasured:
            self.line(f'verify INCOMPLETE: не проверено: {self.unmeasured}', sys.stderr)
        else:
            self.line('verify done')
        return self.code()


def basic(stage):
    """Общее для обеих ролей и аварийка.

    Deploy проверяет экзит сразу после 10-foreign, до стадии 23: на первой
    установке аварийки ещё нет, а при обновлении может стоять стадия 23 до
    образа (пустой штамп, без release.json и Hysteria2). Итоговый verify идёт
    после 23: её штамп «image» требует образ и Hysteria2 строго.
    """
    stage.line('verify — basic checks')
    stage.check('sysctl net.ipv4.ip_forward = 1', host.forwarding)
    stage.check('ufw active', host.firewall)
    for name in ('fail2ban', 'node_exporter', 'sing-box'):
        stage.check(f'{name} active', host.service, name)
    stamp = STAMPS / '23-emergency'
    if not stamp.exists():
        stage.line('аварийка: стадия 23 ещё не ставилась, проверка в итоговом verify')
        return
    stage.check('аварийка: агент vpn2-emergency active', host.service, 'vpn2-emergency.service')
    if emergency.kind(stamp) != 'image':
        stage.line('аварийка: стадия 23 стоит без образа, образ проверит итоговый verify после её обновления')
        return
    stage.image = True
    stage.check('аварийка: образ установлен', emergency.image, EMERGENCY_ROOT, not after_stage())


def after_stage():
    """Стадия 23 этого deploy уже прошла: её штамп новее штампа 10-foreign."""
    return emergency.newer(STAMPS / '23-emergency', STAMPS / '10-foreign')


def youtube(stage):
    """Выключенный YouTube - штатный пропуск; включённый требует A/B-проверку."""
    route = stage.get('YT_ROUTE', 'off')
    if route != 'zapret':
        stage.line(f'yt-zapret: пропущен (youtube.route={route})')
        return
    stage.check('yt-zapret: A/B-проверка обхода', host.command, YT_CHECK, allow_unmeasured=True)
    # Запасной маршрут: при сбое видеопробы YouTube уходит на VPN. Пробу (до
    # 120 с) выполняет таймер; отсутствие свежей пробы - код 2. На чистой
    # установке стадия 19 идёт раньше конфига sing-box, и systemd пропускает
    # пробу по ConditionPathExists - меряем один раз здесь.
    if host.command(*YT_FAILOVER, '--status')[0] == 2:
        host.command('systemctl', 'start', 'anysda-yt-failover.service')
    stage.check('yt-failover: selector и последняя видеопроба', host.command, *YT_FAILOVER, '--status',
                allow_unmeasured=True)
    stage.check('yt-failover: таймер active', host.service, 'anysda-yt-failover.timer')


def entry(stage):
    # После 10/20 первый снимок ещё может относиться к старому туннелю.
    # 25-monitoring задаёт интервал 2 с: 30 с хватает на несколько новых проб.
    stage.check('mgmt scrape: все ожидаемые targets up после свежего скрейпа', host.mgmt_scrape, stage.tag,
                {'ru', *stage.exits}, stage.get('VERIFY_SCRAPE_TIMEOUT', '30'), stage.get('VERIFY_SCRAPE_INTERVAL', '1'))
    if not stage.exits:
        stage.fail('ssh экзитов: EXIT_TAGS пуст — нет узлов для проверки')
    for tag in stage.exits:
        stage.check(f'ssh {tag} через hy2-mgmt', exit_ssh, tag, stage.env.get('SSH_PROBE_TIMEOUT', '30'))
    for label, url in (('frontend: 127.0.0.1:80 отвечает через Caddy', 'http://127.0.0.1:80/'),
                       ('vmsingle: 127.0.0.1:8428 healthy', 'http://127.0.0.1:8428/-/healthy')):
        stage.check(label, host.command, 'curl', '-fsS', '--max-time', '5', '-o', '/dev/null', url)
    stage.check('tproxy: правило fwmark 0x42 → table 101', host.tproxy_rule)
    stage.check('tproxy: local default dev lo в table 101', host.tproxy_route)
    for iface, chain in (('wg0', 'ANYSDA_WG_TPROXY'), ('tun0', 'ANYSDA_OVPN_TPROXY'), ('xfrm0', 'ANYSDA_IKEV2_TPROXY')):
        stage.check(f'tproxy: {iface} цепочка и TCP/UDP правила', host.tproxy_chains, iface, chain)
    queue = host.recv_queue()
    if queue:
        stage.line(f'tproxy udp: Recv-Q {queue} (устойчивый рост означает, что sing-box не читает UDP)')
    else:
        stage.fail('tproxy udp: нет сокета 127.0.0.1:7898 или ss завершился ошибкой')
    stage.check('tproxy wd: таймер active', host.service, 'anysda-tproxy-watchdog.timer')
    stage.check('networkd: drop-in 60-anysda-foreign.conf существует',
                lambda: (0 if NETWORKD_DROPIN.is_file() else 1, ''))
    youtube(stage)
    for name in ('wg-quick@wg0', 'openvpn-server@server', 'strongswan-starter', 'anysda-ikev2-routing'):
        stage.check(f'{name} active', host.service, name)
    stage.check('ikev2: swanctl установлен, conn anysda-ikev2 загружен', host.ikev2_conn)
    stage.check('ikev2: charon загрузил eap-mschapv2', host.ikev2_eap_plugin)
    stage.check('ikev2: udp/500 слушает', host.udp, '500')
    stage.check('ikev2: udp/4500 слушает', host.udp, '4500')
    stage.check('ikev2: интерфейс xfrm0 существует', host.command, 'ip', 'link', 'show', 'xfrm0')
    stage.check('ikev2 sync: таймер active', host.service, 'anysda-ikev2-sync.timer')
    stage.traffic()
    stage.check('ikev2 sync: статус обновлён менее 120 секунд назад', ikev2_status)
    stage.insecure_exits()
    if stage.image:
        stage.check('аварийка: отправка снимка клиентов enabled и active', emergency.sender)


def exit_node(stage):
    port = stage.env.get('HY2_MGMT_PORT')
    if port:
        stage.check(f'hy2-mgmt: udp/{port} слушает', host.udp, port)
    else:
        stage.fail('hy2-mgmt: HY2_MGMT_PORT не задан')
    for name, variable, default in (('direct', 'HY2_DIRECT_PORT', '443'), ('warp', 'HY2_WARP_PORT', '8443')):
        port = stage.get(variable, default)
        stage.check(f'Hysteria2 {name}: udp/{port} слушает', host.udp, port)
    if not stage.image:
        return
    stage.check('аварийка: Hysteria2 в ожидании', emergency.standby, EMERGENCY_HY2,
                EMERGENCY_STATE / 'agent.json', not after_stage())
    # Конфиг TLS появляется со стадией 23 этой версии: verify до её обновления его не ждёт.
    if TLS_CONFIG.is_file():
        stage.check('аварийка: выпуск сертификата на IP (caddy, vpn2-tls.timer) active', emergency.acme)
        # Без сертификата авария работает на самоподписанном с pinSHA256: не ошибка, но видно.
        if not emergency.ip_certificate(TLS_CONFIG, TLS_CERT):
            stage.warning('аварийка: доверенного сертификата на IP экзита пока нет — авария стартует на '
                          'самоподписанном (pinSHA256 в ссылках)')
    # Verify сразу после 10-foreign идёт до стадии 23 на entry: при обновлении
    # со старой версии службы снимка там ещё нет. Снимок проверяет итоговый verify.
    if after_stage():
        stage.check('аварийка: снимок клиентов от entry свежий', emergency.snapshot, EMERGENCY_ROOT,
                    EMERGENCY_STATE, stage.get('VERIFY_SNAPSHOT_WAIT', '90'))
    else:
        stage.line('аварийка: снимок клиентов проверит итоговый verify после стадии 23')


def run(env, *, traffic_only=False):
    """Проверка узла по окружению стадии: код 0, 1 или 2."""
    missing = [name for name in ('HOST_TAG', 'MGMT_IP') if not env.get(name)]
    if missing:
        say('verify: не задано ' + ', '.join(missing), sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix='vpn2-verify-') as directory:
        stage = Stage(env, Path(directory) / 'rows.jsonl')
        if traffic_only:
            stage.traffic()
            return stage.finish()
        basic(stage)
        if stage.tag == 'ru':
            entry(stage)
        else:
            exit_node(stage)
        stage.write_report()
        return stage.finish()
