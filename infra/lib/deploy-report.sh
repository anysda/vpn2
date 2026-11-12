#!/usr/bin/env bash
# Итог полного деплоя: рамка со стадиями и входом в панель, заметки и предупреждения.
# Деплой идёт с entry, поэтому состояние entry читается с диска этой же машины.

print_summary() {
  local total=$(( $(date +%s) - _TOTAL_T0 )) timing url pass title entry row width=0 rows=()
  timing="$(( total / 60 ))m $(( total % 60 ))s"; timing=${timing#0m }
  # shellcheck disable=SC1091
  source "$ENVS/ru.env"
  url="http://${SSH_HOST}/"
  [[ "$(cat /etc/vpn2/tls/panel/secure 2>/dev/null)" != true ]] || url="https://${ENTRY_HOST:-$SSH_HOST}/"
  [[ -z "${PANEL_DOMAIN:-}" ]] || url="https://${PANEL_DOMAIN}/"
  pass="ssh ${SSH_USER:-root}@${SSH_HOST} cat /etc/anysda/admin-password.txt"
  [[ -z "${ADMIN_PASSWORD:-}" ]] || pass='задан в config.yaml (admin.password)'
  title="  деплой завершён  ·  ${timing}  "
  if [[ -n "${_VERIFY_WARNINGS[*]:-}${_DEPLOY_WARNINGS[*]:-}${SKIPPED_EXIT_TAGS:-}" ]]; then
    title="  ⚠ деплой завершён с предупреждениями  ·  ${timing}  "
  fi
  local LC_ALL=C.UTF-8
  mapfile -t rows < <(printf '%s\n' "${_STAGE_LOG[@]}" | column -t -s $'\t' | sed 's/^/  /')
  rows=("$title" - "${rows[@]}" - "  Panel:    $url" "  Login:    ${ADMIN_USER:-admin}" "  Password: $pass")
  for row in "${rows[@]}"; do (( ${#row} <= width )) || width=${#row}; done
  width=$(( width + 12 ))
  local bar; bar=$(printf '─%.0s' $(seq 1 "$width"))
  printf '\n%b  ┌%s┐%b\n' "$C_G" "$bar" "$C_END"
  for row in "${rows[@]}"; do
    if [[ "$row" == - ]]; then
      printf '%b  ├%s┤%b\n' "$C_G" "$bar" "$C_END"
    else
      printf '%b  │%b%s%*s%b│%b\n' "$C_G" "$C_END" "$row" $(( width - ${#row} )) '' "$C_G" "$C_END"
    fi
  done
  printf '%b  └%s┘%b\n\n' "$C_G" "$bar" "$C_END"
  print_ikev2_notice
  print_sso_notice
  if [[ -n "${_VERIFY_WARNINGS[*]:-}" ]]; then
    warn '99-verify: установка завершена, но не все проверки измерены'
    printf '%s\n' "${_VERIFY_WARNINGS[@]}"
  fi
  [[ -z "${_DEPLOY_WARNINGS[*]:-}" ]] || printf '%s\n' "${_DEPLOY_WARNINGS[@]}"
  [[ -z "${SKIPPED_EXIT_TAGS:-}" ]] \
    || HOST_TAG=deploy warn "pre-flight: экзиты пропущены из-за недоступного SSH: $SKIPPED_EXIT_TAGS"
}

print_ikev2_notice() {
  local mode host domain
  if ! { mode=$(cat /etc/anysda/ikev2-mode) && host=$(cat /etc/anysda/ikev2-server-host); } 2>/dev/null; then
    HOST_TAG=ru warn 'IKEv2: не удалось прочитать состояние сервера'
    return 0
  fi
  domain=$(cat /etc/anysda/ikev2-domain 2>/dev/null || true)
  printf 'IKEv2: mode=%s server-host=%s\n' "$mode" "$host"
  if [[ -n "$domain" && ( "$mode" != letsencrypt || "$host" != "$domain" ) ]]; then
    printf 'IKEv2: ожидается сертификат Caddy для %s; автоматическая проверка каждые 30 с.\n' "$domain"
  fi
}

print_sso_notice() (
  # shellcheck disable=SC1091
  source "$ENVS/ru.env"
  secret="${SSO_CLIENT_SECRET:-}"
  if [[ "${SSO_REQUESTED:-${SSO_ENABLED:-false}}" == true && -z "${secret//[[:space:]]/}" && ! -f /etc/anysda/sso-runtime.json ]]; then
    printf '%s\n' 'SSO не включён, потому что не задан секрет. Входите по паролю администратора. Чтобы включить SSO, заполните sso.client_secret в config.yaml и запустите ./deploy.sh 30-frontend ru.'
  fi
)

# Бот включён, если токен и чат заданы в config.yaml или в панели (telegram-runtime.json пишут
# стадия и панель: строка токена, чат строкой или числом).
announce_deploy_done() (
  local runtime=/etc/anysda/telegram-runtime.json
  # shellcheck disable=SC1091
  source "$ENVS/ru.env"
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    grep -Eq '"bot_token": *"[^"]' "$runtime" 2>/dev/null && grep -Eq '"chat_id": *("[^"]|-?[1-9])' "$runtime" || exit 0
  fi
  unmeasured=false; [[ -z "${_VERIFY_WARNINGS[*]:-}" ]] || unmeasured=true
  [[ ! -f /etc/anysda/tgbot-secret.txt ]] || curl -fsS --max-time 5 -H 'Content-Type: application/json' \
    -H "X-Tgbot-Secret: $(cat /etc/anysda/tgbot-secret.txt)" \
    -d "{\"type\":\"deploy_done\",\"verify_unmeasured\":$unmeasured}" http://127.0.0.1:8877/event >/dev/null 2>&1 || true
)

print_banner() {
  local tag rows=('anysda-vpn2 ·  deployment orchestrator' '' 'RU entry    →  sing-box router')
  for tag in $(exit_tags); do rows+=("            →  ${tag^^} экзит"); done
  local LC_ALL=C.UTF-8
  printf '\n%b  ┌──────────────────────────────────────────┐%b\n' "$C_B" "$C_END"
  for tag in "${rows[@]}"; do printf '%b  │%b  %-40s%b│%b\n' "$C_B" "$C_END" "$tag" "$C_B" "$C_END"; done
  printf '%b  └──────────────────────────────────────────┘%b\n\n' "$C_B" "$C_END"
}

usage() {
  cat <<'TEXT'
anysda-vpn2 — деплой

  ./deploy.sh                              полный pipeline: манифест commit → prereqs → проверки → деплой
  ./deploy.sh --manifest <file>             то же без сети: манифест сборки из файла
  ./deploy.sh --development [stage group]   установка из локального дерева, без опубликованного манифеста
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
