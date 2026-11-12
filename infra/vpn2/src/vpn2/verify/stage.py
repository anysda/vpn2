"""Проверка узла после деплоя: 0 успешно, 1 отказ, 2 не проверено.

Строки вывода `[узел] ✓/✗/? проверка` читает deploy и стенд; отчёт для CI
собирается в VERIFY_REPORT, секретов в нём нет. Каждая проверка - команда
узла или функция, отдающая (код, вывод), как команда shell.
"""
import collections
import datetime
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback

from vpn2.watchdog import warp, yt

REPORT = '/var/lib/anysda/verify-report.json'
TRAFFIC = Path(__file__).with_name('traffic.py')
STAMPS = Path('/var/anysda/.stamps')
EMERGENCY_ROOT = Path('/opt/vpn2-emergency')
VENV = Path('/opt/vpn2/venv')
EMERGENCY_STATE = Path('/var/lib/vpn2-emergency')
EMERGENCY_HY2 = Path('/etc/vpn2/emergency-hy2.json')
TLS_CONFIG = Path('/etc/vpn2/tls/config.json')
TLS_CERT = '/etc/vpn2/tls/ip/fullchain.pem'
NETWORKD_DROPIN = Path('/etc/systemd/networkd.conf.d/60-anysda-foreign.conf')
IKEV2_STATUS = Path('/etc/anysda/ikev2-status.json')
SINGBOX_CONFIG = Path('/etc/sing-box/config.json')
WARP_STATE = Path('/var/lib/anysda-warp-watchdog/state.json')
TARGETS_URL = 'http://127.0.0.1:8428/api/v1/targets'
YT_CHECK = '/usr/local/bin/anysda-yt-check'
TRAFFIC_LABEL = 'трафик: контрольный клиент'
TPROXY = ('-j', 'TPROXY', '--tproxy-mark', '0x42/0x42', '--on-port', '7898', '--on-ip', '127.0.0.1')
STATIC = ('static/index.html', 'static/instructions.html')
COPIES = ('vpn2', 'vpn2_emergency')
BROKEN = (OSError, ValueError, KeyError, TypeError, AttributeError)
# Метка начала вывода команды на узле: всё до неё напечатал .bashrc.
MARK = 'vpn2-ssh-output-begin'
NO_MARK_REASON = 'shell узла не дошёл до команды (.bashrc root делает exit или exec), команда не выполнена'
# Снимок читает код самой аварийки под её пользователем: каталог состояния закрыт для остальных.
SNAPSHOT_AGE = '''
import sys
import time
from vpn2.emergency import snapshot, snapshot_sender

state_dir, wait = sys.argv[1], float(sys.argv[2])
limit = 2 * snapshot_sender.RECONCILE_SECONDS
deadline = time.monotonic() + wait
while True:
    age = snapshot.age_seconds(state_dir)
    if (age is not None and age < limit) or time.monotonic() >= deadline:
        break
    time.sleep(min(snapshot_sender.POLL_SECONDS, wait))
if age is None:
    print('снимка клиентов нет: entry его не присылал')
    raise SystemExit(1)
print(f'снимок клиентов подтверждён {age:.0f} с назад, предел {limit} с')
raise SystemExit(0 if age < limit else 1)
'''


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def say(text, stream=None):
    print(text, file=stream or sys.stdout, flush=True)


def problem(error):
    """Причина одной строкой, как последняя строка трассировки."""
    return traceback.format_exception_only(error)[-1].rstrip('\n')


def code(returncode):
    """Код, каким его видит shell: смерть от сигнала N - 128+N."""
    return returncode if returncode >= 0 else 128 - returncode


def sh(*argv, merge=False, env=None):
    """Команда узла: (код, stdout, stderr); merge кладёт stderr в stdout. Нет команды - 127."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                              text=True, errors='replace', env=env)
    except OSError as error:
        reason = f'{argv[0]}: {error.strerror}\n'
        return (127 if isinstance(error, FileNotFoundError) else 126), reason if merge else '', '' if merge else reason
    return code(done.returncode), done.stdout, done.stderr or ''


def command(*argv):
    """Проверка одной командой: её код и весь вывод."""
    rc, out, _ = sh(*argv, merge=True)
    return rc, out.rstrip('\n')


def chain(*commands):
    """Команды через &&: вывод копится, первая упавшая обрывает цепочку."""
    text = ''
    for argv in commands:
        rc, out, _ = sh(*argv, merge=True)
        text += out
        if rc:
            return rc, text.rstrip('\n')
    return 0, text.rstrip('\n')


def grep(argv, pattern, env=None):
    """`команда | grep -E pattern`: 1 без подходящей строки, иначе код команды; вывод - её stderr."""
    rc, out, err = sh(*argv, env=env)
    hit = any(re.search(pattern, line) for line in out.splitlines())
    return (rc if hit else 1), err.rstrip('\n')


def yt_status():
    """Selector YouTube и последняя проба сторожа: 2 - свежего измерения нет, 1 - выход или проба не те."""
    try:
        config = json.loads(yt.CONFIG.read_text())
        if not any(o.get('tag') == 'youtube-auto' for o in config.get('outbounds', [])):
            return 2, 'YouTube: ожидаю конфиг youtube-auto от стадии 20'
        lines = ['YouTube selector: ' + yt.api('GET')['now']]
        state = json.loads(yt.STATE.read_text()) if yt.STATE.exists() else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as failure:
        return 1, f'YouTube: ошибка контроллера {type(failure).__name__}'
    if lines[0].rpartition(' ')[2] not in ('youtube-ru', 'foreign-best'):
        return 1, '\n'.join([*lines, 'неизвестный выход selector'])
    if not isinstance(state, dict):
        return 2, '\n'.join([*lines, 'нет измерений видеопотока' if state is None else 'некорректное состояние видеопробы'])
    counters = [state.get(key) for key in ('good', 'bad')]
    if any(type(value) is not int or not 0 <= value <= yt.HITS for value in counters):
        return 2, '\n'.join([*lines, 'некорректные счётчики видеопробы'])
    detail = state.get('last_probe') or 'нет измерений видеопотока'
    lines.append(f'good={counters[0]} bad={counters[1]}; {"не измерено: " if state.get("unmeasured") else ""}{detail}')
    if state.get('unmeasured') or not any(counters) or not state.get('last_probe'):
        return 2, '\n'.join(lines)
    stamp = state.get('checked_at')
    # Период 5 мин + до 240 с на oneshot; старый успех не скрывает остановку проб.
    if type(stamp) not in (int, float) or not 0 <= time.time() - stamp <= 600:
        return 2, '\n'.join([*lines, 'нет свежего измерения видеопотока (не старше 600 с)'])
    if counters[1]:
        return 1, '\n'.join([*lines, 'последняя проба обхода неуспешна; запасной VPN-путь этой пробой не проверяется'])
    return 0, '\n'.join(lines)


def service(name):
    return command('systemctl', 'is-active', '--quiet', name)


def udp(port):
    return grep(('ss', '-H', '-lun', f'sport = :{port}'), '.')


def firewall():
    rc, text = grep(('ufw', 'status'), '^Status: active$', env={**os.environ, 'LC_ALL': 'C'})
    if rc:
        return rc, text
    rc, out, _ = sh('iptables', '-C', 'INPUT', '-j', 'ufw-before-input', merge=True)
    text += out
    if rc == 0:
        rc, _, err = sh('iptables', '-S', 'ufw-user-input')
        text += err
    return rc, text.rstrip('\n')


def tproxy_chains(iface, chain_name):
    return chain(('iptables', '-t', 'mangle', '-C', 'PREROUTING', '-i', iface, '-j', chain_name),
                 *(('iptables', '-t', 'mangle', '-C', chain_name, '-p', proto, *TPROXY) for proto in ('tcp', 'udp')),
                 ('iptables', '-C', 'INPUT', '-m', 'mark', '--mark', '0x42/0x42', '-j', 'ACCEPT'))


def recv_queue():
    """Recv-Q сокета tproxy 127.0.0.1:7898 или None: сокета нет или ss упал."""
    rc, out, _ = sh('ss', '-H', '-uln', 'sport = :7898')
    for line in out.splitlines() if rc == 0 else ():
        fields = line.split()
        if len(fields) >= 4 and fields[3].startswith('127.0.0.1:'):
            return fields[1] or None
    return None


def ikev2_eap_plugin():
    """Плагин в статистике живого charon, а не наличие .so или загрузка плагина самим CLI."""
    rc, out, err = sh('swanctl', '--stats')
    loaded = [name for found in re.finditer(r'(?m)^\s*loaded plugins: *(.*)$', out)
              for name in found[1].split(' ') if name == 'eap-mschapv2']
    return (rc if loaded else 1), (err + '\n'.join(loaded)).rstrip('\n')


def ikev2_status():
    """Ошибка чтения, JSON и время из будущего не должны давать свежий статус."""
    try:
        age = time.time() - json.loads(IKEV2_STATUS.read_text())['updated']
    except BROKEN as error:
        return 1, problem(error)
    return (0 if 0 <= age < 120 else 1), f'ikev2 status age: {age:.0f}s'


def targets_problem(raw, since, expected):
    """Что не так со списком targets vmsingle; None - все ждут up и скрейп свежее since."""
    try:
        data = json.loads(raw)
        targets = data['data']['activeTargets']
        if data['status'] != 'success' or not isinstance(targets, list) or not targets:
            raise ValueError('пустой/некорректный список targets')
        seen, failures = set(), []
        for target in targets:
            host = target['labels'].get('host', '?')
            seen.add(host)
            stamp = datetime.datetime.fromisoformat(target['lastScrape'].replace('Z', '+00:00'))
            fresh = stamp.tzinfo is not None and since <= stamp.timestamp() <= time.time()
            if target['health'] != 'up' or not fresh:
                failures.append(f'{host}: health={target["health"]}, свежий={fresh}')
        failures.extend(f'{host}: отсутствует' for host in sorted(expected - seen))
        if failures:
            raise ValueError('; '.join(failures))
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        return f'targets: {error}'
    return None


def mgmt_scrape(tag, expected, timeout, interval):
    """Ждёт, пока все ожидаемые targets станут up по скрейпу свежее начала проверки."""
    since, deadline = time.time(), time.monotonic() + float(timeout)
    lines = [f'[{tag}] mgmt scrape: жду свежие targets up (до {timeout} с)']
    detail = 'API targets недоступен'
    while (left := deadline - time.monotonic()) > 0:
        rc, raw, err = sh('curl', '-fsS', '--max-time', str(max(1, min(5, math.ceil(left)))), TARGETS_URL)
        lines += err.splitlines()
        detail = targets_problem(raw, since, expected) if rc == 0 else 'API targets недоступен'
        if detail is None:
            return 0, '\n'.join(lines + [f'[{tag}] mgmt scrape: все targets up, скрейпы свежее начала проверки'])
        if time.monotonic() < deadline:
            time.sleep(float(interval))
    return 1, '\n'.join(lines + [f'[{tag}] mgmt scrape: таймаут — {detail}'])


def exit_ssh(tag, limit):
    """Вход на экзит через hy2-mgmt под общим пределом: (код, вывод команды после метки или причина).

    Login shell экзита читает .bashrc до команды: вывод до метки отбрасывается, без
    метки команда не выполнена. ConnectTimeout ограничивает только соединение, поэтому
    вход идёт в своей сессии, и по пределу гибнет вся сессия. Вывод - в файлы:
    осиротевший потомок с открытым каналом не держит чтение.
    """
    if not re.fullmatch(r'[1-9][0-9]{0,5}', limit):
        return 1, f"SSH_PROBE_TIMEOUT='{limit}': нужно целое число секунд от 1 до 999999 без ведущего нуля"
    argv = ['ssh', '-n', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', tag, f"printf '\\n%s\\n' {MARK}"]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        try:
            rc, expired = code(process.wait(timeout=int(limit))), False
        except subprocess.TimeoutExpired:
            expired = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        if expired:
            process.wait()
            return 124, f'экзит не ответил за предел {limit} с: вход по ssh или .bashrc root на экзите завис'
        out.seek(0)
        err.seek(0)
        stdout, stderr = (stream.read().decode(errors='replace').rstrip('\n') for stream in (out, err))
    # stderr .bashrc приходит от sshd отдельным каналом: показываем только сообщение ssh о соединении.
    if rc == 255:
        return 255, stderr
    if f'\n{MARK}\n' not in f'\n{stdout}\n':
        return 1, NO_MARK_REASON
    return rc, stdout[stdout.index(MARK) + len(MARK):].removeprefix('\n')


def after_stage():
    """Стадия 23 этого deploy уже прошла: её штамп новее штампа 10-foreign или того нет."""
    try:
        own = (STAMPS / '23-emergency').stat().st_mtime_ns
    except OSError:
        return False
    try:
        return own > (STAMPS / '10-foreign').stat().st_mtime_ns
    except OSError:
        return True


def stamp_kind(stamp):
    """Первое слово штампа стадии 23: «image» пишет стадия, ставящая образ."""
    try:
        line, newline, _ = Path(stamp).read_text().partition('\n')
    except (OSError, ValueError):
        return ''
    return (line.split() or [''])[0] if newline else ''


def emergency_image(root, venv, before_stage):
    """Версия аварийного образа и раскладка стадии 23; до неё прежняя раскладка - не отказ."""
    try:
        release = json.loads((root / 'release.json').read_text())
        lines = [f"аварийный образ: версия {release['version']}, ревизия {release['revision'][:12]}, "
                 f"{release['image'][:19]}"]
    except BROKEN as error:
        return 1, problem(error)
    copies = [name for name in COPIES if (root / name).exists()]
    if copies and before_stage:
        return 0, '\n'.join(lines + ['прежняя раскладка до стадии 23: новую проверит итоговый verify'])
    problems = [f'нет {name}' for name in STATIC if not (root / name).is_file()]
    if not os.access(venv / 'bin/vpn2', os.X_OK):
        problems.append(f'нет {venv}/bin/vpn2')
    problems += [f'осталась копия пакета {root / name}' for name in copies]
    if problems:
        return 1, '\n'.join(lines + ['аварийка: ' + ', '.join(problems)])
    return 0, '\n'.join(lines)


def emergency_standby(options, agent_state, before_stage):
    """Аварийный Hysteria2 ждёт выключенным; до стадии 23 при идущей аварии порт может слушать."""
    try:
        port = int(json.loads(Path(options).read_text())['port'])
    except BROKEN as error:
        return 1, problem(error)
    rc, listeners, err = sh('ss', '-H', '-uln', f'sport = :{port}')
    if rc:
        return rc, err.rstrip('\n')
    try:
        leader = json.loads(Path(agent_state).read_text()).get('leader', {}).get('active') is True
    except BROKEN:
        leader = False
    if listeners.strip('\n') and before_stage and leader:
        return 0, err + f'идёт аварийный режим: udp/{port} слушает до стадии 23, закрытым его проверит итоговый verify'
    if listeners.strip('\n'):
        return 1, err + f'udp/{port} уже слушает'
    return 0, err + f'аварийный Hysteria2: udp/{port} закрыт'


def ip_certificate(config, cert):
    """Есть ли доверенный сертификат на IP экзита со сроком больше часа."""
    try:
        host = json.loads(Path(config).read_text())['host']
    except BROKEN:
        return False
    rc, _ = chain(('openssl', 'verify', '-purpose', 'sslserver', '-verify_ip', str(host), '-untrusted', cert, cert),
                  ('openssl', 'x509', '-in', cert, '-checkend', '3600', '-noout'))
    return rc == 0


def snapshot(venv, state_dir, wait):
    """Снимок клиентов от entry: предел - два периода отправки, первый ждём не дольше wait."""
    return command('setpriv', '--reuid=vpn2-emergency', '--regid=vpn2-emergency', '--init-groups', '--',
                   f'{venv}/bin/python', '-B', '-c', SNAPSHOT_AGE, str(state_dir), str(wait))


def warp_trouble(config=None, state=None):
    """Почему канал warp экзита не работает; None - работает или не узнать."""
    try:
        if not warp.enabled(json.loads(Path(config or SINGBOX_CONFIG).read_text())):
            return 'профиля Cloudflare нет, регистрация не прошла'
        dead = 'bad_since' in json.loads(Path(state or WARP_STATE).read_text())
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return 'Cloudflare не отвечает, сторож повторяет попытки' if dead else None


def active(*units):
    return chain(*(('systemctl', 'is-active', '--quiet', unit) for unit in units))


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

    def checks(self, table):
        for label, probe, *args in table:
            self.check(label, probe, *args)

    def warning(self, text):
        """Предупреждение не меняет код, но идёт в итог и в отчёт при любом исходе."""
        self.line(f'⚠ {text}', sys.stderr)
        self.warnings.append(text)

    def traffic(self):
        """Настоящий трафик контрольным клиентом; строки отчёта модуль дописывает сам."""
        module = Path(self.get('VERIFY_TRAFFIC', TRAFFIC))
        if not module.is_file():
            self.fail(TRAFFIC_LABEL, f'модуль {module.name} не найден')
            return
        self.line('трафик: контрольный клиент в netns (до нескольких минут)')
        before = len(self.read_rows())
        rc = code(subprocess.run(
            [sys.executable, '-I', str(module), 'check', '--tag', self.tag, '--exits', ' '.join(self.exits),
             '--report', str(self.rows)]).returncode)
        # Итог считает строки модуля: три «Ошибка» - три провала.
        fresh = [row['result'] for row in self.read_rows()[before:]]
        failed, unmeasured = fresh.count('Ошибка'), fresh.count('Не проверено')
        if rc == 2:
            self.unmeasured += unmeasured or 1
        elif rc:
            self.failures += failed or 1
            self.unmeasured += unmeasured
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
            say(problem(error), sys.stderr)
            self.line(f'✗ отчёт: не записан {path}', sys.stderr)
            self.failures += 1
            return
        counts = collections.Counter(row['result'] for row in rows)
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
    """Общее для обеих ролей и аварийка; образ аварийки строго требует итоговый verify после стадии 23."""
    stage.line('verify — basic checks')
    stage.checks([('sysctl net.ipv4.ip_forward = 1', grep, ('sysctl', '-n', 'net.ipv4.ip_forward'), '^1$'),
                  ('ufw active', firewall),
                  *((f'{name} active', service, name) for name in ('fail2ban', 'node_exporter', 'sing-box'))])
    stamp = STAMPS / '23-emergency'
    if not stamp.exists():
        stage.line('аварийка: стадия 23 ещё не ставилась, проверка в итоговом verify')
        return
    stage.check('аварийка: агент vpn2-emergency active', service, 'vpn2-emergency.service')
    if stamp_kind(stamp) != 'image':
        stage.line('аварийка: стадия 23 стоит без образа, образ проверит итоговый verify после её обновления')
        return
    stage.image = True
    stage.check('аварийка: образ установлен', emergency_image, EMERGENCY_ROOT, VENV, not after_stage())


def youtube(stage):
    """Выключенный YouTube - штатный пропуск; включённый требует A/B-проверку и запасной маршрут."""
    route = stage.get('YT_ROUTE', 'off')
    if route != 'zapret':
        stage.line(f'yt-zapret: пропущен (youtube.route={route})')
        return
    stage.check('yt-zapret: A/B-проверка обхода', command, YT_CHECK, allow_unmeasured=True)
    # Нет свежей видеопробы (код 2) - одна проба здесь: таймер мог её ещё не запускать.
    if yt_status()[0] == 2:
        command('systemctl', 'start', 'anysda-yt-failover.service')
    stage.check('yt-failover: selector и последняя видеопроба', yt_status, allow_unmeasured=True)
    stage.check('yt-failover: таймер active', service, 'anysda-yt-failover.timer')


def entry(stage):
    stage.check('mgmt scrape: все ожидаемые targets up после свежего скрейпа', mgmt_scrape, stage.tag,
                {'ru', *stage.exits}, stage.get('VERIFY_SCRAPE_TIMEOUT', '30'), stage.get('VERIFY_SCRAPE_INTERVAL', '1'))
    if not stage.exits:
        stage.fail('ssh экзитов: EXIT_TAGS пуст — нет узлов для проверки')
    curl = ('curl', '-fsS', '--max-time', '5', '-o', '/dev/null')
    stage.checks([
        *((f'ssh {tag} через hy2-mgmt', exit_ssh, tag, stage.env.get('SSH_PROBE_TIMEOUT', '30')) for tag in stage.exits),
        ('frontend: 127.0.0.1:80 отвечает через Caddy', command, *curl, 'http://127.0.0.1:80/'),
        ('vmsingle: 127.0.0.1:8428 healthy', command, *curl, 'http://127.0.0.1:8428/-/healthy'),
        ('tproxy: правило fwmark 0x42 → table 101', grep, ('ip', 'rule', 'list'), r'fwmark 0x42 lookup 101(\s|$)'),
        ('tproxy: local default dev lo в table 101', grep, ('ip', 'route', 'show', 'table', '101'),
         r'^local default dev lo(\s|$)'),
        *((f'tproxy: {iface} цепочка и TCP/UDP правила', tproxy_chains, iface, chain_name) for iface, chain_name in
          (('wg0', 'ANYSDA_WG_TPROXY'), ('tun0', 'ANYSDA_OVPN_TPROXY'), ('xfrm0', 'ANYSDA_IKEV2_TPROXY')))])
    queue = recv_queue()
    if queue:
        stage.line(f'tproxy udp: Recv-Q {queue} (устойчивый рост означает, что sing-box не читает UDP)')
    else:
        stage.fail('tproxy udp: нет сокета 127.0.0.1:7898 или ss завершился ошибкой')
    stage.checks([('tproxy wd: таймер active', service, 'anysda-tproxy-watchdog.timer'),
                  ('networkd: drop-in 60-anysda-foreign.conf существует',
                   lambda: (0 if NETWORKD_DROPIN.is_file() else 1, ''))])
    youtube(stage)
    stage.checks([
        *((f'{name} active', service, name)
          for name in ('wg-quick@wg0', 'openvpn-server@server', 'strongswan-starter', 'anysda-ikev2-routing')),
        ('ikev2: swanctl установлен, conn anysda-ikev2 загружен', grep, ('swanctl', '--list-conns'), '^anysda-ikev2:'),
        ('ikev2: charon загрузил eap-mschapv2', ikev2_eap_plugin),
        ('ikev2: udp/500 слушает', udp, '500'),
        ('ikev2: udp/4500 слушает', udp, '4500'),
        ('ikev2: интерфейс xfrm0 существует', command, 'ip', 'link', 'show', 'xfrm0'),
        ('ikev2 sync: таймер active', service, 'anysda-ikev2-sync.timer')])
    stage.traffic()
    stage.check('ikev2 sync: статус обновлён менее 120 секунд назад', ikev2_status)
    for tag in stage.exits:
        if stage.env.get(tag.upper().replace('-', '_') + '_HY2_INSECURE') == '1':
            stage.warning(f'{tag}: exits[].hy2_insecure: true — сертификат экзита не проверяется, '
                          'подмену экзита на пути entry → экзит не видно')
    if stage.image:
        stage.check('аварийка: отправка снимка клиентов enabled и active', chain,
                    ('systemctl', 'is-enabled', '--quiet', 'vpn2-emergency-snapshot.service'),
                    ('systemctl', 'is-active', '--quiet', 'vpn2-emergency-snapshot.service'))


def exit_node(stage):
    port = stage.env.get('HY2_MGMT_PORT')
    if port:
        stage.check(f'hy2-mgmt: udp/{port} слушает', udp, port)
    else:
        stage.fail('hy2-mgmt: HY2_MGMT_PORT не задан')
    for name, variable, default in (('direct', 'HY2_DIRECT_PORT', '443'), ('warp', 'HY2_WARP_PORT', '8443')):
        port = stage.get(variable, default)
        stage.check(f'Hysteria2 {name}: udp/{port} слушает', udp, port)
    trouble = warp_trouble()
    if trouble:
        stage.warning(f'WARP: {trouble} — экзит работает без канала warp; '
                      f'повтор: ./deploy.sh 10-foreign {stage.tag}')
    if not stage.image:
        return
    stage.check('аварийка: Hysteria2 в ожидании', emergency_standby, EMERGENCY_HY2,
                EMERGENCY_STATE / 'agent.json', not after_stage())
    # Конфиг TLS ставит стадия 23: до её обновления его нет.
    if TLS_CONFIG.is_file():
        stage.check('аварийка: выпуск сертификата на IP (caddy, vpn2-tls.timer) active', active,
                    'caddy.service', 'vpn2-tls.timer')
        if not ip_certificate(TLS_CONFIG, TLS_CERT):
            stage.warning('аварийка: доверенного сертификата на IP экзита пока нет — авария стартует на '
                          'самоподписанном (pinSHA256 в ссылках)')
    # Службу снимка на entry ставит стадия 23: до неё снимок проверит итоговый verify.
    if after_stage():
        stage.check('аварийка: снимок клиентов от entry свежий', snapshot, VENV,
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
