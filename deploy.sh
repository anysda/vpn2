#!/usr/bin/env bash
# anysda-vpn — точка входа
#
# Использование:
#   ./deploy.sh                    весь pipeline по порядку (prereqs → check → all)
#   ./deploy.sh <stage> <group>    одна стадия на группу нод

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Явный выбор разработки; остальные запуски требуют опубликованный манифест.
if [[ "${1:-}" == --development ]]; then
  export VPN2_DEVELOPMENT=true
  shift
elif [[ "${1:-}" == --manifest ]]; then
  export VPN2_DEVELOPMENT=false
  export VPN2_RELEASE_MANIFEST="$(realpath "${2:?manifest path required}")"
  shift 2
fi

# Справка оркестратора не требует конфига и генерации env.
case "${1:-}" in
  -h|--help) exec "${REPO_ROOT}/infra/deploy.sh" "$@" ;;
esac

if [[ -t 1 ]]; then
  G='\033[32m' R='\033[31m' Y='\033[33m' E='\033[0m'
else
  G= R= Y= E=
fi

die()  { printf '%b✗ %s%b\n' "$R" "$*" "$E" >&2; exit 1; }
ok()   { printf '%b✓ %s%b\n' "$G" "$*" "$E"; }

# Protect generated envs before the entry-side, cross-workstation lock is taken.
mkdir -p "$REPO_ROOT/infra/state"
exec {local_deploy_fd}>"$REPO_ROOT/infra/state/deploy.lock"
flock -n "$local_deploy_fd" || die 'deploy уже выполняется из этого рабочего дерева'

# ── Проверка config.yaml ──────────────────────────────────────────────────────
if [[ ! -f "${REPO_ROOT}/config.yaml" ]]; then
  printf '\n  %b config.yaml не найден%b\n\n' "$R" "$E"
  printf '  Запусти мастер настройки:\n'
  printf '  %b./setup.sh%b\n\n' "$Y" "$E"
  exit 1
fi

# ── Генерация env-файлов из config.yaml ─────────────────────────────────────
python3 "${REPO_ROOT}/infra/lib/config2env.py" "${REPO_ROOT}" \
  || die "Ошибка при генерации env-файлов из config.yaml"

# ── Делегируем в оркестратор ─────────────────────────────────────────────────
# Без аргументов: install_prereqs → check → full deploy.
# С аргументами: точечный запуск (например `./deploy.sh 30-frontend ru`).
exec "${REPO_ROOT}/infra/deploy.sh" "$@"
