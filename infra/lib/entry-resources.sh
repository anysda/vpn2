# shellcheck shell=bash
# Библиотека лежит рядом со стадией при деплое, в lib/ при restore.
ENTRY_RESOURCE_TOOL=$(dirname "${BASH_SOURCE[0]}")/entry_resources.py

entry_resources() {
  local rendered
  rendered=$(ENTRY_RESOURCE_OVERRIDES="${ENTRY_RESOURCE_OVERRIDES:-}" \
    python3 "$ENTRY_RESOURCE_TOOL" --service "$1") || return
  eval "$rendered"
}

entry_adguard_resources() {
  ENTRY_RESOURCE_OVERRIDES="${ENTRY_RESOURCE_OVERRIDES:-}" \
    python3 "$ENTRY_RESOURCE_TOOL" --agh-config "$@"
}

# Пишет drop-in, только если тело сменилось; смену отмечает ENTRY_DROPIN_CHANGED=1.
entry_dropin_write() {
  local dir=$1 rendered=$2 file="$1/zz-vpn2-resources.conf"
  if [[ -f "$file" ]] && printf '%s\n' "$rendered" | cmp -s - "$file"; then
    return 0
  fi
  mkdir -p "$dir" && printf '%s\n' "$rendered" > "$file.tmp" && mv -f "$file.tmp" "$file" || return
  ENTRY_DROPIN_CHANGED=1
}

# OOMScoreAdjust и Nice systemd ставит только при запуске: живым процессам юнита
# выставляем те же значения сами, без перезапуска (он рвёт клиентов).
entry_unit_live() {
  local unit=$1 rendered=$2 oom nice cgroup pid
  oom=$(sed -n 's/^OOMScoreAdjust=//p' <<< "$rendered")
  nice=$(sed -n 's/^Nice=//p' <<< "$rendered")
  [[ -n "$oom$nice" ]] || return 0
  cgroup=$(systemctl show -p ControlGroup --value "$unit.service" 2>/dev/null) || return 0
  [[ -n "$cgroup" ]] || return 0
  cgroup="${ENTRY_CGROUP_ROOT:-/sys/fs/cgroup}$cgroup/cgroup.procs"
  [[ -r "$cgroup" ]] || return 0
  while read -r pid; do
    if [[ -n "$oom" ]]; then
      echo "$oom" 2>/dev/null > "${ENTRY_PROC_ROOT:-/proc}/$pid/oom_score_adj" || true
    fi
    if [[ -n "$nice" ]]; then
      renice -n "$nice" -p "$pid" >/dev/null 2>&1 || true
    fi
  done < "$cgroup"
}

# Свой drop-in последним по имени: перекрывает ручной override.conf, не трогая его.
# daemon-reload только при смене файла; перезапуск остаётся за стадией.
entry_unit_resources() {
  local unit=${1%.service} rendered slice root
  ENTRY_DROPIN_CHANGED=0
  root=${ENTRY_UNIT_DIR:-/etc/systemd/system}
  rendered=$(ENTRY_RESOURCE_OVERRIDES="${ENTRY_RESOURCE_OVERRIDES:-}" \
    python3 "$ENTRY_RESOURCE_TOOL" --unit "$unit") || return
  entry_dropin_write "$root/$unit.service.d" "$rendered" || return
  if grep -q '^MemoryLow=' <<< "$rendered"; then
    slice=$(python3 "$ENTRY_RESOURCE_TOOL" --unit system.slice) || return
    entry_dropin_write "$root/system.slice.d" "$slice" || return
  fi
  if [[ "$ENTRY_DROPIN_CHANGED" == 1 ]]; then
    systemctl daemon-reload || return
  fi
  entry_unit_live "$unit" "$rendered"
}
