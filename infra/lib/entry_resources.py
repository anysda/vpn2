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
    import yaml  # На entry ставится bootstrap; расчёт и config2env без PyYAML.

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-path", type=Path, default=Path("/var/lib/vmsingle"))
    parser.add_argument("--meminfo", type=Path, default=Path("/proc/meminfo"))
    parser.add_argument("--ram-mib", type=int)
    parser.add_argument("--free-mib", type=int)
    parser.add_argument("--service", choices=("panel", "bot", "vm", "agh"))
    parser.add_argument("--agh-config", type=Path)
    parser.add_argument("--agh-state", type=Path, default=Path("/etc/anysda/agh-resources.json"))
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    ram, free = hardware(args.data_path, args.meminfo)
    overrides = json.loads(os.environ.get("ENTRY_RESOURCE_OVERRIDES") or "{}")
    values = calculate(args.ram_mib if args.ram_mib is not None else ram,
                       args.free_mib if args.free_mib is not None else free, overrides)
    if args.agh_config:
        if patch_adguard(args.agh_config, args.agh_state, values, overrides, args.fresh, args.check):
            print("changed")
    else:
        print(shell(values, args.service) if args.service else json.dumps(values, sort_keys=True))


if __name__ == "__main__":
    main()
