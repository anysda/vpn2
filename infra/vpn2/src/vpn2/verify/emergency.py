"""Проверки аварийки. Агент - пакет vpn2 из venv хоста, стадия 23 кладёт из
образа статику страницы: версия образа и всё, что нужно активации, лежат на
узле заранее (docker для проверки не будим).

Verify сразу после 10-foreign идёт до стадии 23 этого deploy (before_stage):
при обновлении на узле ещё лежит прежняя аварийка с копией пакета рядом
(vpn2_emergency у выпуска, vpn2 у сборок до venv). Её заменит стадия 23,
новую раскладку строго требует итоговый verify.
"""
import json
import os
from pathlib import Path
import traceback

from vpn2.verify.host import chain, command, sh

STATIC = ('static/index.html', 'static/instructions.html')
COPIES = ('vpn2', 'vpn2_emergency')
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
BROKEN = (OSError, ValueError, KeyError, TypeError, AttributeError)


def newer(path, other):
    """`[[ path -nt other ]]`: path новее other или other нет."""
    try:
        own = os.stat(path).st_mtime_ns
    except OSError:
        return False
    try:
        return own > os.stat(other).st_mtime_ns
    except OSError:
        return True


def kind(stamp):
    """Первое слово штампа стадии 23: «image» пишет стадия, ставящая образ."""
    try:
        line, newline, _ = Path(stamp).read_text().partition('\n')
    except (OSError, ValueError):
        return ''
    return (line.split() or [''])[0] if newline else ''


def problem(error):
    """Причина одной строкой, как последняя строка трассировки."""
    return traceback.format_exception_only(error)[-1].rstrip('\n')


def image(root, venv, before_stage):
    lines = []
    try:
        release = json.loads((root / 'release.json').read_text())
        lines.append(f"аварийный образ: версия {release['version']}, ревизия {release['revision'][:12]}, "
                     f"{release['image'][:19]}")
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


def leader_active(agent_state):
    try:
        return json.loads(Path(agent_state).read_text()).get('leader', {}).get('active') is True
    except BROKEN:
        return False


def standby(options, agent_state, before_stage):
    """Аварийный Hysteria2 ждёт выключенным: его порт никто не слушает.

    Исключение - verify сразу после 10-foreign при идущей аварии: entry
    переустановлен и ещё не знаком экзиту. Аварию завершит агент после
    стадии 23 этого deploy, итоговый verify требует порт закрытым.
    """
    try:
        port = int(json.loads(Path(options).read_text())['port'])
    except BROKEN as error:
        return 1, problem(error)
    rc, listeners, err = sh('ss', '-H', '-uln', f'sport = :{port}')
    if rc:
        return rc, err.rstrip('\n')
    if listeners.strip('\n') and before_stage and leader_active(agent_state):
        return 0, err + f'идёт аварийный режим: udp/{port} слушает до стадии 23, закрытым его проверит итоговый verify'
    if listeners.strip('\n'):
        return 1, err + f'udp/{port} уже слушает'
    return 0, err + f'аварийный Hysteria2: udp/{port} закрыт'


def acme():
    """Сертификат на IP экзита выпускает Caddy, выкладывает vpn2-tls.timer."""
    return chain(('systemctl', 'is-active', '--quiet', 'caddy.service'),
                 ('systemctl', 'is-active', '--quiet', 'vpn2-tls.timer'))


def ip_certificate(config, cert):
    """Есть ли доверенный сертификат на IP экзита со сроком больше часа."""
    try:
        host = json.loads(Path(config).read_text())['host']
    except BROKEN:
        return False
    rc, _ = chain(('openssl', 'verify', '-purpose', 'sslserver', '-verify_ip', str(host), '-untrusted', cert, cert),
                  ('openssl', 'x509', '-in', cert, '-checkend', '3600', '-noout'))
    return rc == 0


def sender():
    """Снимок клиентов для аварийки рассылает служба entry."""
    return chain(('systemctl', 'is-enabled', '--quiet', 'vpn2-emergency-snapshot.service'),
                 ('systemctl', 'is-active', '--quiet', 'vpn2-emergency-snapshot.service'))


def snapshot(venv, state_dir, wait):
    """Экзит ждёт подтверждения снимка раз в период отправки: предел - два периода,
    первый снимок после установки или перерыва туннеля ждём не дольше wait."""
    return command('setpriv', '--reuid=vpn2-emergency', '--regid=vpn2-emergency', '--init-groups', '--',
                   f'{venv}/bin/python', '-B', '-c', SNAPSHOT_AGE, str(state_dir), str(wait))
