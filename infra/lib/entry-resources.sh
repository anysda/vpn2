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
