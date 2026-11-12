#!/usr/bin/env python3
"""Ресурсы entry: расчёт на целевой машине, явные overrides, владение полями AGH.

MiB в config.yaml; 0 снимает лимит (штатная автоматика runtime).
RAM-классы допускают 1/16 памяти, зарезервированной ядром/гипервизором.
До 4 ГБ бюджеты удваиваются на ступень: 1/2/3 ГБ = 1/2, 1, 2 от
профиля 2 ГБ. На 4 ГБ+ не добавляем ограничений к поведению origin/dev.
Retention: 1/3/5/7 суток по RAM, не более (free GiB - 1) суток по диску,
минимум сутки. 1 GiB оставляем разделу, 1 GiB/сутки — консервативный
бюджет, а не оценка фактической компрессии; merge требует отдельного замера.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import shlex


ADAWAY_URL = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_2.txt"


def calculate(ram_mib, free_mib, overrides):
    if ram_mib <= 0 or free_mib < 0:
        raise ValueError("RAM должна быть положительной, свободный диск — неотрицательным")
    tier = min(4, max(1, ram_mib // 960))
    factor = 2 ** (tier - 2)
    result = {key: int(base * factor) if tier < 4 else 0 for key, base in {
        "panel_memory_mib": 384, "panel_old_space_mib": 256,
        "bot_memory_mib": 128, "vm_memory_mib": 256,
        "vm_cache_mib": 64, "vm_query_mib": 16,
        "vm_concurrent_requests": 4, "agh_memory_mib": 256,
        "agh_high_mib": 192, "agh_go_mib": 160,
    }.items()}
    result.update(retention_days=min(2 * tier - 1, max(1, free_mib // 1024 - 1)),
                  adaway_enabled=tier >= 3 and free_mib >= 2048,
                  querylog_size=(50, 100, 500, 1000)[tier - 1])
    unknown = overrides.keys() - result.keys()
    if unknown:
        raise ValueError(f"Неизвестные entry_resources: {sorted(unknown)}")
    for key, value in overrides.items():
        if key == "adaway_enabled":
            if str(value).lower() not in ("true", "false"):
                raise ValueError(f"{key}: требуется true/false")
            result[key] = str(value).lower() == "true"
        else:
            if not str(value).isdigit() or (key == "retention_days" and int(value) == 0):
                raise ValueError(f"{key}: требуется целое {'> 0' if key == 'retention_days' else '>= 0'}")
            result[key] = int(value)
    return result


def hardware(data_path, meminfo):
    ram = next(int(line.split()[1]) // 1024 for line in meminfo.read_text().splitlines()
               if line.startswith("MemTotal:"))
    # Первый прогон: каталога ещё нет. Ближайший существующий родитель
    # относится к тому же разделу; отдельный /var/lib/vmsingle учитывается.
    while not data_path.exists():
        data_path = data_path.parent
    disk = os.statvfs(data_path)
    return ram, disk.f_bavail * disk.f_frsize // (1024 * 1024)


def shell(values, service):
    docker, runtime = [], []
    memory = values.get(f"{service}_memory_mib", 0)
    if service != "agh" and memory:
        docker += ["--memory", f"{memory}m", "--memory-swap", f"{memory}m"]
    if service == "panel" and values["panel_old_space_mib"]:
        docker += ["-e", f"NODE_OPTIONS=--max-old-space-size={values['panel_old_space_mib']}"]
    if service == "vm":
        runtime.append(f"-retentionPeriod={values['retention_days']}d")
        for key, flag in (("vm_cache_mib", "memory.allowedBytes"),
                          ("vm_query_mib", "search.maxMemoryPerQuery"),
                          ("vm_concurrent_requests", "search.maxConcurrentRequests")):
            if values[key]:
                runtime.append(f"-{flag}={values[key]}{'MiB' if key.endswith('_mib') else ''}")
    lines = [f"ENTRY_DOCKER_ARGS=({shlex.join(docker)})",
             f"ENTRY_RUNTIME_ARGS=({shlex.join(runtime)})"]
    if service == "agh":
        limits = {"AGH_MEMORY_MAX": f"{memory}M" if memory else "infinity",
                  "AGH_MEMORY_HIGH": f"{values['agh_high_mib']}M" if values['agh_high_mib'] else "infinity",
                  "AGH_GO_LIMIT": f"{values['agh_go_mib']}MiB" if values['agh_go_mib'] else "off",
                  "AGH_SWAP_MAX": "0" if memory else "infinity",
                  "AGH_ADAWAY": str(values['adaway_enabled']).lower(),
                  "AGH_QUERYLOG": str(values['querylog_size'])}
        lines += [f"{key}={shlex.quote(value)}" for key, value in limits.items()]
    return "\n".join(lines)


def patch_adguard(path, state_path, values, overrides, fresh=False, check=False):
    import yaml  # На entry PyYAML ставит bootstrap; расчёт без него.

    cfg = yaml.safe_load(path.read_text())
    before = copy.deepcopy(cfg)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    managed = {}
    fields = [("querylog_size", cfg.setdefault("querylog", {}), "size_memory")]
    fields += [("adaway_enabled", item, "enabled") for item in cfg.get("filters") or []
               if item.get("id") == 2 and item.get("url") == ADAWAY_URL]
    for key, section, field in fields:
        # Без истории нельзя отличить старый дефолт от осознанного выбора.
        # Не присваиваем себе существующие настройки; config.yaml имеет приоритет.
        if (fresh or key in overrides or field not in section
                or (key in state and section[field] == state[key])):
            section[field] = values[key]
            managed[key] = values[key]
    changed = cfg != before
    if not check:
        if changed:
            path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
        if managed != state or not state_path.exists():
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(managed, sort_keys=True) + "\n")
            state_path.chmod(0o600)
    return changed


# Юниты entry: какой лимит памяти и сколько MiB на ступени 2 ГБ. На других
# ступенях объём умножается так же, как бюджеты docker; лимиты остаются и на
# 4 ГБ+: это потолок против утечки, а не подгонка.
# MemoryLow - трафик (sing-box, charon, openvpn): под давлением отдаётся последним;
# MemoryMax - долгоживущее и пробы: на 2 ГБ сумма с docker и AGH не больше
#   MemTotal (724 + 1280 MiB); на 1 ГБ сумма больше памяти из-за пола пробы YouTube;
# MemoryHigh - короткий oneshot: в сумму MemoryMax не входит.
UNITS = {
    "sing-box": ("MemoryLow", 128),
    "strongswan-starter": ("MemoryLow", 64),
    "openvpn-server@server": ("MemoryLow", 64),
    "node_exporter": ("MemoryMax", 48),
    "anysda-yt-nfqws": ("MemoryMax", 32),
    "vpn2-emergency": ("MemoryMax", 96),
    "anysda-failover-watchdog": ("MemoryMax", 48),
    "vpn2-emergency-snapshot": ("MemoryMax", 96),
    "anysda-yt-failover": ("MemoryMax", 340),
    "anysda-tproxy-watchdog": ("MemoryMax", 32),
    **{unit: ("MemoryHigh", 256) for unit in (
        "anysda-fq", "anysda-mgmt-loopback", "anysda-hy2hop", "anysda-warp-watchdog",
        "anysda-yt-nft", "anysda-yt-offload", "anysda-yt-quic", "anysda-yt-quic-refresh",
        "anysda-apply-routes", "anysda-doh-routing", "vpn2-tls", "anysda-backup",
        "anysda-ikev2-routing", "anysda-ikev2-sync", "anysda-ikev2-cert-sync",
        "anysda-wg-routing", "anysda-ovpn-routing")},
}
# Проба YouTube: yt-dlp + node решают JS-задачу YouTube, пик cgroup 301-311 МиБ,
# на 300M node убивается каждый раз. Пол 340 на любой ступени; убитая по лимиту
# проба не измеряет, но и маршрут не трогает.
MEMORY_FLOOR = {"anysda-yt-failover": 340}
# Аварийный агент и отправитель снимка держат ~32 МБ сутками (тест на тысячу
# раундов), с дочерним collect - до ~55: потолок один на любой ступени, MemoryHigh
# тормозит утечку раньше, чем MemoryMax её убьёт. Те же числа в самих юнитах.
FIXED_MEMORY = {"vpn2-emergency": (64, 96), "vpn2-emergency-snapshot": (64, 96)}
# Проба YouTube, сторожа, snapshot, backup: уступают CPU и диск трафику.
BACKGROUND = {"anysda-yt-failover", "anysda-yt-quic-refresh", "anysda-failover-watchdog",
              "anysda-tproxy-watchdog", "anysda-warp-watchdog", "vpn2-emergency-snapshot",
              "anysda-backup"}


def unit_memory(ram_mib, unit):
    if unit in FIXED_MEMORY:
        return FIXED_MEMORY[unit][1]
    tier = min(4, max(1, ram_mib // 960))
    return max(MEMORY_FLOOR.get(unit, 0), int(UNITS[unit][1] * 2 ** (tier - 2)))


def unit_dropin(ram_mib, unit):
    # С memory_recursiveprot MemoryLow ребёнка действует не выше защиты предков:
    # system.slice получает сумму защищаемых юнитов той же ступени.
    if unit == "system.slice":
        protected = sum(unit_memory(ram_mib, name) for name, (limit, _) in UNITS.items()
                        if limit == "MemoryLow")
        return f"[Slice]\nMemoryLow={protected}M"
    if unit not in UNITS:
        raise ValueError(f"Неизвестный юнит entry: {unit}")
    limit = UNITS[unit][0]
    lines = ["[Service]", f"{limit}={unit_memory(ram_mib, unit)}M"]
    if limit == "MemoryLow":
        lines += ["OOMScoreAdjust=-900", "CPUWeight=1000", "IOWeight=1000"]
    elif limit == "MemoryMax":
        if unit in FIXED_MEMORY:
            lines.insert(1, f"MemoryHigh={FIXED_MEMORY[unit][0]}M")
        lines.append("MemorySwapMax=0")
    if unit in BACKGROUND:
        lines += ["CPUWeight=20", "IOWeight=20", "Nice=10"]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-path", type=Path, default=Path("/var/lib/vmsingle"))
    parser.add_argument("--meminfo", type=Path, default=Path("/proc/meminfo"))
    parser.add_argument("--ram-mib", type=int)
    parser.add_argument("--free-mib", type=int)
    parser.add_argument("--service", choices=("panel", "bot", "vm", "agh"))
    parser.add_argument("--agh-config", type=Path)
    parser.add_argument("--unit", help="юнит без .service: печатает тело drop-in")
    parser.add_argument("--agh-state", type=Path, default=Path("/etc/anysda/agh-resources.json"))
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    ram, free = hardware(args.data_path, args.meminfo)
    overrides = json.loads(os.environ.get("ENTRY_RESOURCE_OVERRIDES") or "{}")
    values = calculate(args.ram_mib if args.ram_mib is not None else ram,
                       args.free_mib if args.free_mib is not None else free, overrides)
    if args.unit:
        print(unit_dropin(args.ram_mib if args.ram_mib is not None else ram, args.unit))
    elif args.agh_config:
        if patch_adguard(args.agh_config, args.agh_state, values, overrides, args.fresh, args.check):
            print("changed")
    else:
        print(shell(values, args.service) if args.service else json.dumps(values, sort_keys=True))


if __name__ == "__main__":
    main()
