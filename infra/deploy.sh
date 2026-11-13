#!/usr/bin/env bash
# anysda-vpn2 - деплой с entry: флаги, справка, проверка config.yaml; дальше оркестратор
# из пакета vpn2 (infra/vpn2/src/vpn2/deploy) системным python3, до сборки venv.
set -euo pipefail

# Юнит systemd без User= и systemd-run переменную HOME не задают, а ключ оркестратора живёт в ней.
: "${HOME:=$(getent passwd "$(id -u)" | cut -d: -f6)}"
[[ -n "$HOME" ]] || { echo 'deploy: у пользователя нет домашнего каталога (HOME пуст, в passwd его нет)' >&2; exit 1; }
DEPLOY_ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO=$(dirname "$DEPLOY_ROOT")
export HOME DEPLOY_ROOT
if [[ -t 2 ]]; then R='\033[31m' Y='\033[33m' E='\033[0m'; else R='' Y='' E=''; fi

usage() {
  cat <<'TEXT'
anysda-vpn2 — деплой

  ./deploy.sh                              полный pipeline: манифест commit → prereqs → проверки → деплой
  ./deploy.sh --manifest <file>             то же без сети: манифест сборки из файла
  ./deploy.sh --development [stage group]   явный режим разработки с локальными/плавающими образами
  ./deploy.sh <stage> <group>              запустить одну стадию на группу нод
  ./deploy.sh backup                       снять зашифрованный backup сейчас
  ./deploy.sh restore [<name>|latest] [--force] [--dry-run]
                                           восстановить из backup (default: latest)
  ./deploy.sh backup-list                  список доступных backup'ов
  ./deploy.sh recover                      проверенный полный повтор после прерывания
  ./deploy.sh rollback                    совместимый откат панели без отката данных

Стадии:   00-bootstrap | 05-mgmt-mesh | 10-foreign | 28-wireguard |
          29-openvpn | 19-yt-zapret | 20-ru-router | 21-failover-watchdog |
          22-adguard | 23-emergency | 25-monitoring | 26-backup | 27-ikev2 | 35-telegram | 30-frontend |
          29-control | 99-verify
Группы:   ru | foreign | all | <конкретный тег экзита>
TEXT
  exit 0
}

# Без --development запуск сверяется с манифестом сборки: опубликованным или из --manifest.
MANIFEST=${VPN2_RELEASE_MANIFEST:-}
case "${1:-}" in
  --development) VPN2_DEVELOPMENT=true; shift ;;
  --manifest)
    [[ -f "${2:-}" ]] || { printf '%b✗ --manifest <файл>: файл манифеста не найден: %s%b\n' "$R" "${2:-}" "$E" >&2; exit 1; }
    VPN2_DEVELOPMENT=false MANIFEST=$(realpath "$2")
    shift 2 ;;
esac
case "${1:-}" in -h|--help) usage ;; esac
if [[ ! -f "$REPO/config.yaml" ]]; then
  printf '\n  %b config.yaml не найден%b\n\n' "$R" "$E"
  printf '  Запусти мастер настройки:\n'
  printf '  %b./setup.sh%b\n\n' "$Y" "$E"
  exit 1
fi
export VPN2_DEVELOPMENT=${VPN2_DEVELOPMENT:-false} VPN2_MANIFEST=$MANIFEST
PYTHONPATH="$DEPLOY_ROOT/vpn2/src" exec python3 -B -m vpn2.deploy "$@"
