#!/usr/bin/env bash
# anysda-vpn — точка входа
#
# Использование:
#   ./deploy.sh                    весь pipeline по порядку (prereqs → check → all)
#   ./deploy.sh <stage> <group>    одна стадия на группу нод

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

if [[ -t 1 ]]; then
  G='\033[32m' R='\033[31m' Y='\033[33m' E='\033[0m'
else
  G= R= Y= E=
fi

die()  { printf '%b✗ %s%b\n' "$R" "$*" "$E" >&2; exit 1; }
ok()   { printf '%b✓ %s%b\n' "$G" "$*" "$E"; }

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
