#!/usr/bin/env bash
# anysda-vpn - точка входа деплоя: замок запуска и перенос состояния, затем infra/deploy.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
[[ "${1:-}" != @(-h|--help) ]] || exec "${REPO_ROOT}/infra/deploy.sh" "$@"

if [[ -t 1 ]]; then R='\033[31m' E='\033[0m'; else R='' E=''; fi
die() { printf '%b✗ %s%b\n' "$R" "$*" "$E" >&2; exit 1; }

# shellcheck source=infra/lib/state-dir.sh
source "$REPO_ROOT/infra/lib/state-dir.sh"
# Каталог состояния только для root: панель (свой uid в том же /var/lib/anysda-vpn2) known_hosts не читает.
install -d -m 700 "$VPN2_STATE_DIR" 2>/dev/null \
  || die "нет доступа к $VPN2_STATE_DIR: deploy.sh запускается от root"
exec {local_deploy_fd}>"$VPN2_DEPLOY_LOCK" \
  || die "нет доступа к $VPN2_DEPLOY_LOCK: deploy.sh запускается от root"
flock -n "$local_deploy_fd" || die 'deploy уже выполняется на этой машине'
state_dir_migrate "$REPO_ROOT/infra" \
  || die "состояние деплоя не перенеслось из infra/state в $VPN2_STATE_DIR"

# Замок держит только этот процесс; оркестратор идёт без его дескриптора и получает TERM,
# если этот процесс умер.
setpriv --pdeathsig TERM "${REPO_ROOT}/infra/deploy.sh" "$@" {local_deploy_fd}>&-
