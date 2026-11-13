#!/usr/bin/env bash
# anysda-vpn - точка входа деплоя: замок запуска, затем infra/deploy.sh (перенос состояния - в нём).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
# Справка - без замка, root и конфига, в том числе после --development и --manifest <файл>.
help=${1:-}; [[ "$help" != --development ]] || help=${2:-}; [[ "${1:-}" != --manifest ]] || help=${3:-}
[[ "$help" != @(-h|--help) ]] || exec "${REPO_ROOT}/infra/deploy.sh" "$@"

if [[ -t 1 ]]; then R='\033[31m' E='\033[0m'; else R='' E=''; fi
die() { printf '%b✗ %s%b\n' "$R" "$*" "$E" >&2; exit 1; }

# Состояние деплоя вне checkout (входит в бэкап); замок запуска - не состояние.
export VPN2_STATE_DIR=${VPN2_STATE_DIR:-/var/lib/anysda-vpn2/state} VPN2_DEPLOY_LOCK=${VPN2_DEPLOY_LOCK:-/run/lock/anysda-vpn2-deploy.lock}
# Каталог состояния только для root: панель (свой uid в том же /var/lib/anysda-vpn2) known_hosts не читает.
install -d -m 700 "$VPN2_STATE_DIR" 2>/dev/null \
  || die "нет доступа к $VPN2_STATE_DIR: deploy.sh запускается от root"
exec {local_deploy_fd}>"$VPN2_DEPLOY_LOCK" \
  || die "нет доступа к $VPN2_DEPLOY_LOCK: deploy.sh запускается от root"
flock -n "$local_deploy_fd" || die 'deploy уже выполняется на этой машине'

# Замок держит только этот процесс; оркестратор идёт без его дескриптора и получает TERM,
# если этот процесс умер.
setpriv --pdeathsig TERM "${REPO_ROOT}/infra/deploy.sh" "$@" {local_deploy_fd}>&-
