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

# Свой drop-in последним по имени: перекрывает ручной override.conf, не трогая его.
# daemon-reload только при смене файла; перезапуск остаётся за стадией.
entry_unit_resources() {
  local unit=${1%.service} rendered dir file
  dir="${ENTRY_UNIT_DIR:-/etc/systemd/system}/$unit.service.d"
  file="$dir/zz-vpn2-resources.conf"
  rendered=$(ENTRY_RESOURCE_OVERRIDES="${ENTRY_RESOURCE_OVERRIDES:-}" \
    python3 "$ENTRY_RESOURCE_TOOL" --unit "$unit") || return
  if [[ -f "$file" ]] && printf '%s\n' "$rendered" | cmp -s - "$file"; then
    return 0
  fi
  mkdir -p "$dir" || return
  printf '%s\n' "$rendered" > "$file.tmp" && mv -f "$file.tmp" "$file" || return
  systemctl daemon-reload
}
