#!/usr/bin/env bash
# Восстановление из бэкапа (vpn2.backup.stages) зовёт стадию напрямую из клона
# репозитория: секрет бота из архива сходится с панелью.
# Установку ведёт роль containers, шаг у неё тот же.
set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] || { echo "35-telegram: нужен файл окружения" >&2; exit 2; }
set -a
# shellcheck disable=SC1090
source "$1"
set +a
: "${HOST_TAG:?}"
case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 35-telegram только для entry - пропуск"; exit 0;; esac

root=$(cd "$(dirname "$0")/../.." && pwd)
export VPN2_ENTRY_RESOURCES="$root/infra/lib/entry_resources.py"
export VPN2_PANEL_CONFIG_TPL="$root/infra/configs/anysda-config.yaml.tpl"
export VPN2_DEPLOY_STATE="$root/infra/lib/deploy_state.py"
exec "${VPN2_BIN:-/opt/vpn2/venv/bin/vpn2}" containers telegram
