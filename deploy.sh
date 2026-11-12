#!/usr/bin/env bash
# anysda-vpn — точка входа
#
# Использование:
#   ./deploy.sh                    весь pipeline по порядку (prereqs → check → all)
#   ./deploy.sh <stage> <group>    одна стадия на группу нод

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Явный выбор разработки; остальные запуски проверяются манифестом сборки:
# по умолчанию опубликованным для commit дерева, --manifest - из файла (без сети).
if [[ "${1:-}" == --development ]]; then
  export VPN2_DEVELOPMENT=true
  shift
elif [[ "${1:-}" == --manifest ]]; then
  export VPN2_DEVELOPMENT=false
  [[ -f "${2:-}" ]] || { echo "✗ --manifest <файл>: файл манифеста не найден: ${2:-}" >&2; exit 1; }
  export VPN2_RELEASE_MANIFEST="$(realpath "$2")"
  shift 2
fi

# Справка оркестратора не требует конфига и генерации env.
case "${1:-}" in
  -h|--help) exec "${REPO_ROOT}/infra/deploy.sh" "$@" ;;
esac

if [[ -t 1 ]]; then
  R='\033[31m' Y='\033[33m' E='\033[0m'
else
  R= Y= E=
fi

die()  { printf '%b✗ %s%b\n' "$R" "$*" "$E" >&2; exit 1; }

# Состояние деплоя живёт вне checkout, замок - в /run/lock. Замок берётся до
# генерации env; под ним же состояние старой раскладки переезжает из infra/.
# shellcheck source=infra/lib/state-dir.sh
source "$REPO_ROOT/infra/lib/state-dir.sh"
state_dir_ensure 2>/dev/null \
  || die "нет доступа к $VPN2_STATE_DIR: deploy.sh запускается от root"
exec {local_deploy_fd}>"$VPN2_DEPLOY_LOCK" \
  || die "нет доступа к $VPN2_DEPLOY_LOCK: deploy.sh запускается от root"
flock -n "$local_deploy_fd" || die 'deploy уже выполняется на этой машине'
state_dir_migrate "$REPO_ROOT/infra" \
  || die "состояние деплоя не перенеслось из infra/state в $VPN2_STATE_DIR"

# ── Проверка config.yaml ──────────────────────────────────────────────────────
if [[ ! -f "${REPO_ROOT}/config.yaml" ]]; then
  printf '\n  %b config.yaml не найден%b\n\n' "$R" "$E"
  printf '  Запусти мастер настройки:\n'
  printf '  %b./setup.sh%b\n\n' "$Y" "$E"
  exit 1
fi

PYTHONPATH="$REPO_ROOT/infra/vpn2/src" python3 -B "$REPO_ROOT/infra/ansible/inventory.py" --prepare || exit 1
# Каталог окружений прежних выпусков (с секретами) не читается никем.
rm -rf "$REPO_ROOT/infra/envs"

# ── Делегируем в оркестратор ─────────────────────────────────────────────────
# Без аргументов: install_prereqs → check → full deploy.
# С аргументами: точечный запуск (например `./deploy.sh 30-frontend ru`).
# Замок держит только этот процесс и отпускает его своим выходом: оркестратор
# идёт потомком без дескриптора замка, иначе замок наследовал бы каждый его
# фоновый помощник и держал бы ещё секунды после выхода деплоя. Умер этот
# процесс (в том числе от KILL) - оркестратор получает TERM.
rc=0
setpriv --pdeathsig TERM "${REPO_ROOT}/infra/deploy.sh" "$@" {local_deploy_fd}>&- || rc=$?
exit "$rc"
