"""Ресурсы узла: бюджеты памяти по RAM, drop-in юнитов и поля журнала AdGuard.

MiB в config.yaml (entry_resources); 0 снимает лимит. Ступень - RAM / 960 MiB (1..4):
до 4 ГБ бюджеты ступени 2 ГБ делятся или умножаются на 2, с 4 ГБ контейнеры без лимитов.
Retention: 1/3/5/7 суток по RAM, не больше (свободных GiB - 1), минимум сутки.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from vpn2 import fsutil

ADAWAY_URL = 'https://adguardteam.github.io/HostlistsRegistry/assets/filter_2.txt'
BUDGETS = {'panel_memory_mib': 384, 'panel_old_space_mib': 256, 'bot_memory_mib': 128,
           'vm_memory_mib': 256, 'vm_cache_mib': 64, 'vm_query_mib': 16, 'vm_concurrent_requests': 4,
           'agh_memory_mib': 256, 'agh_high_mib': 192, 'agh_go_mib': 160}
DROPIN = 'zz-vpn2-resources.conf'
UNIT_DIR = Path('/etc/systemd/system')

# MemoryLow - трафик, под давлением отдаётся последним; MemoryMax - долгоживущее и пробы;
# MemoryHigh - короткий oneshot. MiB на ступени 2 ГБ, на других - так же, как бюджеты.
UNITS = {
    'sing-box': ('MemoryLow', 128),
    'strongswan-starter': ('MemoryLow', 64),
    'openvpn-server@server': ('MemoryLow', 64),
    'node_exporter': ('MemoryMax', 48),
    'anysda-yt-nfqws': ('MemoryMax', 32),
    'vpn2-emergency': ('MemoryMax', 96),
    'anysda-failover-watchdog': ('MemoryMax', 48),
    'vpn2-emergency-snapshot': ('MemoryMax', 96),
    'anysda-yt-failover': ('MemoryMax', 340),
    'anysda-tproxy-watchdog': ('MemoryMax', 32),
    **{unit: ('MemoryHigh', 256) for unit in (
        'anysda-fq', 'anysda-mgmt-loopback', 'anysda-hy2hop', 'anysda-warp-watchdog',
        'anysda-yt-nft', 'anysda-yt-offload', 'anysda-yt-quic', 'anysda-yt-quic-refresh',
        'anysda-apply-routes', 'anysda-doh-routing', 'vpn2-tls', 'anysda-backup',
        'anysda-ikev2-routing', 'anysda-ikev2-sync', 'anysda-ikev2-cert-sync',
        'anysda-wg-routing', 'anysda-ovpn-routing')},
}
# Проба YouTube (yt-dlp + node) берёт до ~311 MiB на любой ступени.
MEMORY_FLOOR = {'anysda-yt-failover': 340}
# Аварийный агент и отправитель снимка: (MemoryHigh, MemoryMax) на любой ступени.
FIXED_MEMORY = {'vpn2-emergency': (64, 96), 'vpn2-emergency-snapshot': (64, 96)}
BACKGROUND = {'anysda-yt-failover', 'anysda-yt-quic-refresh', 'anysda-failover-watchdog',
              'anysda-tproxy-watchdog', 'anysda-warp-watchdog', 'vpn2-emergency-snapshot',
              'anysda-backup'}


def tier(ram_mib):
    return min(4, max(1, ram_mib // 960))


def calculate(ram_mib, free_mib, overrides):
    if ram_mib <= 0 or free_mib < 0:
        raise ValueError('RAM должна быть положительной, свободный диск — неотрицательным')
    step = tier(ram_mib)
    result = {key: int(base * 2 ** (step - 2)) if step < 4 else 0 for key, base in BUDGETS.items()}
    result.update(retention_days=min(2 * step - 1, max(1, free_mib // 1024 - 1)),
                  adaway_enabled=step >= 3 and free_mib >= 2048,
                  querylog_size=(50, 100, 500, 1000)[step - 1])
    # Значения уже проверил разбор config.yaml (vpn2.config).
    for key, value in overrides.items():
        result[key] = value.lower() == 'true' if key == 'adaway_enabled' else int(value)
    return result


def hardware(data_path=Path('/var/lib/vmsingle'), meminfo=Path('/proc/meminfo')):
    """(RAM MiB, свободно MiB на разделе данных VictoriaMetrics или ближайшего родителя)."""
    ram = next(int(line.split()[1]) // 1024 for line in meminfo.read_text().splitlines()
               if line.startswith('MemTotal:'))
    while not data_path.exists():
        data_path = data_path.parent
    disk = os.statvfs(data_path)
    return ram, disk.f_bavail * disk.f_frsize // (1024 * 1024)


def entry(environ):
    return calculate(*hardware(), json.loads(environ.get('ENTRY_RESOURCE_OVERRIDES') or '{}'))


def container(values, service):
    """Лимиты службы compose, её env и аргументы программы: service - panel, bot или vm."""
    memory = values[f'{service}_memory_mib']
    limits = {'mem_limit': f'{memory}m', 'memswap_limit': f'{memory}m'} if memory else {}
    env, runtime = {}, []
    if service == 'panel' and values['panel_old_space_mib']:
        env['NODE_OPTIONS'] = f"--max-old-space-size={values['panel_old_space_mib']}"
    if service == 'vm':
        runtime.append(f"-retentionPeriod={values['retention_days']}d")
        for key, flag in (('vm_cache_mib', 'memory.allowedBytes'), ('vm_query_mib', 'search.maxMemoryPerQuery'),
                          ('vm_concurrent_requests', 'search.maxConcurrentRequests')):
            if values[key]:
                runtime.append(f"-{flag}={values[key]}{'MiB' if key.endswith('_mib') else ''}")
    return limits, env, runtime


def adguard(values):
    """Лимиты юнитов AdGuard и профиль журнала новой установки."""
    memory, high, go = values['agh_memory_mib'], values['agh_high_mib'], values['agh_go_mib']
    return {'AGH_MEMORY_MAX': f'{memory}M' if memory else 'infinity',
            'AGH_MEMORY_HIGH': f'{high}M' if high else 'infinity',
            'AGH_GO_LIMIT': f'{go}MiB' if go else 'off',
            'AGH_SWAP_MAX': '0' if memory else 'infinity',
            'AGH_ADAWAY': str(values['adaway_enabled']).lower(),
            'AGH_QUERYLOG': str(values['querylog_size'])}


def patch_adguard(cfg, state, values, overrides, fresh=False):
    """Журнал запросов и AdAway в конфиге AdGuard; вернуть, что ведёт деплой.

    Поле берётся под деплой на новой установке, из config.yaml, когда его нет или
    в нём всё ещё записанное деплоем; иначе это выбор админа в панели.
    """
    managed = {}
    fields = [('querylog_size', cfg.setdefault('querylog', {}), 'size_memory')]
    fields += [('adaway_enabled', item, 'enabled') for item in cfg.get('filters') or []
               if item.get('id') == 2 and item.get('url') == ADAWAY_URL]
    for key, section, field in fields:
        if fresh or key in overrides or field not in section or (key in state and section[field] == state[key]):
            section[field] = values[key]
            managed[key] = values[key]
    return managed


def unit_memory(ram_mib, unit):
    if unit in FIXED_MEMORY:
        return FIXED_MEMORY[unit][1]
    return max(MEMORY_FLOOR.get(unit, 0), int(UNITS[unit][1] * 2 ** (tier(ram_mib) - 2)))


def unit_dropin(ram_mib, unit):
    """Тело drop-in; system.slice защищает сумму MemoryLow (memory_recursiveprot)."""
    if unit == 'system.slice':
        protected = sum(unit_memory(ram_mib, name) for name, (limit, _) in UNITS.items() if limit == 'MemoryLow')
        return f'[Slice]\nMemoryLow={protected}M'
    if unit not in UNITS:
        raise ValueError(f'Неизвестный юнит entry: {unit}')
    limit = UNITS[unit][0]
    lines = ['[Service]', f'{limit}={unit_memory(ram_mib, unit)}M']
    if limit == 'MemoryLow':
        lines += ['OOMScoreAdjust=-900', 'CPUWeight=1000', 'IOWeight=1000']
    elif limit == 'MemoryMax':
        if unit in FIXED_MEMORY:
            lines.insert(1, f'MemoryHigh={FIXED_MEMORY[unit][0]}M')
        lines.append('MemorySwapMax=0')
    if unit in BACKGROUND:
        lines += ['CPUWeight=20', 'IOWeight=20', 'Nice=10']
    return '\n'.join(lines)


def _write_dropin(name, body, root):
    path = root / name / DROPIN
    path.parent.mkdir(parents=True, exist_ok=True)
    return fsutil.publish(path, body + '\n', 0o644)


def _live(unit, body):
    """OOMScoreAdjust и Nice systemd ставит при запуске: живым процессам - сразу, без перезапуска."""
    settings = dict(line.split('=', 1) for line in body.splitlines()[1:])
    oom, nice = settings.get('OOMScoreAdjust'), settings.get('Nice')
    if not (oom or nice):
        return
    cgroup = subprocess.run(['systemctl', 'show', '-p', 'ControlGroup', '--value', f'{unit}.service'],
                            capture_output=True, text=True, check=False).stdout.strip()
    if not cgroup:
        return
    root = Path('/sys/fs/cgroup' + cgroup)
    try:
        pids = (root / 'cgroup.procs').read_text().split()
        threads = (root / 'cgroup.threads').read_text().split()
    except OSError:
        return
    for pid in pids if oom else ():
        try:
            Path(f'/proc/{pid}/oom_score_adj').write_text(oom)
        except OSError:
            if Path(f'/proc/{pid}').is_dir():
                print(f'WARN: oom_score_adj {pid}: запись не прошла', file=sys.stderr)
    for tid in threads if nice else ():
        try:
            os.setpriority(os.PRIO_PROCESS, int(tid), int(nice))
        except OSError:
            pass


def apply_units(units, root=UNIT_DIR):
    """Drop-in последним по имени поверх ручного override.conf; вернуть, менялся ли хоть один."""
    ram = hardware()[0]
    changed = False
    for unit in (name.removesuffix('.service') for name in units):
        body = unit_dropin(ram, unit)
        written = _write_dropin(f'{unit}.service.d', body, root)
        if body.splitlines()[1].startswith('MemoryLow='):
            written |= _write_dropin('system.slice.d', unit_dropin(ram, 'system.slice'), root)
        if written:
            subprocess.run(['systemctl', 'daemon-reload'], check=True)
        _live(unit, body)
        changed |= written
    return changed
