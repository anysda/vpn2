#!/usr/bin/env bash
# Восстановление из бэкапа (vpn2.backup.restore) зовёт стадию напрямую из клона
# репозитория: пароль панели из архива попадает в конфиги AdGuard, изменившийся
# экземпляр перезапускается. Установку ведёт роль adguard, шаги у неё те же.
set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] || { echo "22-adguard: нужен файл окружения" >&2; exit 2; }
set -a
# shellcheck disable=SC1090
source "$1"
set +a
: "${HOST_TAG:?}" "${AGH_PORT:?}"
case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 22-adguard только для entry - пропуск"; exit 0;; esac

root=$(cd "$(dirname "$0")/../.." && pwd)
export VPN2_ENTRY_RESOURCES="$root/infra/lib/entry_resources.py" VPN2_POOLS="$root/app/shared/vpn-pools.json"
vpn2=${VPN2_BIN:-/opt/vpn2/venv/bin/vpn2}
"$vpn2" adguard config
"$vpn2" adguard restart
