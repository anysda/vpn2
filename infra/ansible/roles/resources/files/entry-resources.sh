# shellcheck shell=bash
# Библиотека лежит рядом со стадией при деплое, в lib/ при restore.
ENTRY_RESOURCE_TOOL=$(dirname "${BASH_SOURCE[0]}")/entry_resources.py

entry_resources() {
  local rendered
  rendered=$(ENTRY_RESOURCE_OVERRIDES="${ENTRY_RESOURCE_OVERRIDES:-}" \
    python3 "$ENTRY_RESOURCE_TOOL" --service "$1") || return
  eval "$rendered"
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
# oom_score_adj общий на процесс, nice у каждого потока свой: renice -p меняет
# только главный поток, поэтому nice ставится всем tid из cgroup.threads.
# Исчезнувший процесс пропускается молча, отказ записи живому - предупреждение.
entry_unit_live() {
  local unit=$1 rendered=$2 oom nice cgroup pid tid task
  local proc=${ENTRY_PROC_ROOT:-/proc}
  oom=$(sed -n 's/^OOMScoreAdjust=//p' <<< "$rendered")
  nice=$(sed -n 's/^Nice=//p' <<< "$rendered")
  [[ -n "$oom$nice" ]] || return 0
  cgroup=$(systemctl show -p ControlGroup --value "$unit.service" 2>/dev/null) || return 0
  [[ -n "$cgroup" ]] || return 0
  cgroup="${ENTRY_CGROUP_ROOT:-/sys/fs/cgroup}$cgroup"
  [[ -r "$cgroup/cgroup.procs" ]] || return 0
  while read -r pid; do
    if [[ -n "$oom" ]] && ! { echo "$oom" > "$proc/$pid/oom_score_adj"; } 2>/dev/null \
        && [[ -d "$proc/$pid" ]]; then
      echo "WARN: oom_score_adj $pid: запись не прошла" >&2
    fi
    if [[ -n "$nice" && ! -r "$cgroup/cgroup.threads" ]]; then
      task=$(ls "$proc/$pid/task" 2>/dev/null) || task=
      for tid in ${task:-$pid}; do
        renice -n "$nice" -p "$tid" >/dev/null 2>&1 || true
      done
    fi
  done < "$cgroup/cgroup.procs"
  if [[ -n "$nice" && -r "$cgroup/cgroup.threads" ]]; then
    while read -r tid; do
      renice -n "$nice" -p "$tid" >/dev/null 2>&1 || true
    done < "$cgroup/cgroup.threads"
  fi
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
