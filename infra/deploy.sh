#!/usr/bin/env bash
# anysda-vpn2 — оркестратор деплоя
#
# Использование:
#   ./deploy.sh                   полный pipeline: prereqs → проверки → деплой
#   ./deploy.sh <stage> <group>   одна стадия на группу нод
#
# Стадии:   00-bootstrap | 05-mgmt-mesh | 10-foreign | 20-ru-router |
#           22-adguard   | 25-monitoring | 35-telegram | 30-frontend | 99-verify
# Группы:   ru | foreign | all | <конкретный тег экзита>

set -euo pipefail

DEPLOY_ROOT="$(cd "$(dirname "$0")" && pwd)"
export DEPLOY_ROOT

# shellcheck source=lib/ssh.sh
source "$DEPLOY_ROOT/lib/ssh.sh"

# Accumulated stage log for the final summary (populated by run_stage)
_STAGE_LOG=()
_TOTAL_T0=0

# ----------------------------------------------------------------------------
# Banner
# ----------------------------------------------------------------------------
print_banner() {
  local exits; exits=$(exit_tags 2>/dev/null || echo "")
  printf '\n'
  printf '%b  ┌──────────────────────────────────────────┐%b\n' "$C_B" "$C_END"
  printf '%b  │%b  anysda-vpn2 ·  deployment orchestrator  %b│%b\n' "$C_B" "$C_END" "$C_B" "$C_END"
  printf '%b  │%b                                          %b│%b\n' "$C_B" "$C_END" "$C_B" "$C_END"
  printf '%b  │%b  RU entry    →  sing-box router          %b│%b\n' "$C_B" "$C_END" "$C_B" "$C_END"
  if [[ -n "$exits" ]]; then
    for t in $exits; do
      local upper; upper=$(printf '%s' "$t" | tr a-z A-Z)
      printf '%b  │%b              →  %-3s экзит                %b│%b\n' "$C_B" "$C_END" "$upper" "$C_B" "$C_END"
    done
  fi
  printf '%b  └──────────────────────────────────────────┘%b\n' "$C_B" "$C_END"
  printf '\n'
}

# ----------------------------------------------------------------------------
# Summary printed after do_all
# ----------------------------------------------------------------------------
print_summary() {
  local total=$(( $(date +%s) - _TOTAL_T0 ))
  local mins=$(( total / 60 )) secs=$(( total % 60 ))
  local timing="${mins}m ${secs}s"
  [[ "$mins" -eq 0 ]] && timing="${secs}s"

  printf '\n'
  printf '%b  ┌────────────────────────────────────────────────┐%b\n' "$C_G" "$C_END"
  printf '%b  │%b  деплой завершён  ·  %-6s                 %b│%b\n' "$C_G" "$C_END" "$timing" "$C_G" "$C_END"
  printf '%b  ├────────────────────────────────────────────────┤%b\n' "$C_G" "$C_END"
  for entry in "${_STAGE_LOG[@]}"; do
    printf '%b  │%b  %s%b  │%b\n' "$C_G" "$C_END" "$entry" "$C_G" "$C_END"
  done
  printf '%b  ├────────────────────────────────────────────────┤%b\n' "$C_G" "$C_END"
  # shellcheck disable=SC1091
  local _ru_host _panel_url _agh_url
  source "$DEPLOY_ROOT/envs/all.env" 2>/dev/null || true
  source "$DEPLOY_ROOT/envs/ru.env"  2>/dev/null || true
  _ru_host="${SSH_HOST:-${ENTRY_HOST:-}}"
  if [[ -n "${PANEL_DOMAIN:-}" ]]; then
    _panel_url="https://${PANEL_DOMAIN}/"
  else
    _panel_url="http://${_ru_host}/"
  fi
  _agh_url="http://${_ru_host}:3001/"
  printf '%b  │%b  Panel:    %-38s %b│%b\n' "$C_G" "$C_END" "$_panel_url" "$C_G" "$C_END"
  printf '%b  │%b  AdGuard:  %-38s %b│%b\n' "$C_G" "$C_END" "$_agh_url"  "$C_G" "$C_END"
  printf '%b  └────────────────────────────────────────────────┘%b\n' "$C_G" "$C_END"
  printf '\n'
}

# ----------------------------------------------------------------------------
# Установка локальных пререквизитов (на самом оркестраторе).
# Apt-пакеты + Docker (для pull/run, build больше не нужен).
# Идемпотентна — если всё уже стоит, no-op за секунду.
# ----------------------------------------------------------------------------
install_prereqs() {
  printf '%b==>%b проверяю локальные зависимости\n' "$C_B" "$C_END"
  local missing=()
  for cmd in ssh scp sshpass envsubst curl python3 rsync; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    if [[ ! -f /etc/debian_version ]]; then
      die "не хватает: ${missing[*]}. На не-Debian/Ubuntu поставь руками."
    fi
    printf '  ставлю недостающее: %s\n' "${missing[*]}"
    # envsubst живёт в gettext-base
    local apt_pkgs="${missing[*]/envsubst/gettext-base}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      $apt_pkgs ca-certificates >/dev/null
  fi

  if ! command -v docker >/dev/null 2>&1; then
    if [[ ! -f /etc/debian_version ]]; then
      die "docker не установлен; на не-Debian/Ubuntu поставь руками."
    fi
    printf '  ставлю docker (через get.docker.com)\n'
    curl -fsSL https://get.docker.com | sh >/dev/null 2>&1
    systemctl enable --now docker >/dev/null 2>&1 || true
  fi

  for cmd in ssh scp sshpass envsubst curl python3 rsync docker; do
    printf '  %-10s %b✓%b\n' "$cmd" "$C_G" "$C_END"
  done
}

# ----------------------------------------------------------------------------
# Проверка env-файлов + SSH-доступа на всех нодах. die при первой ошибке.
# ----------------------------------------------------------------------------
do_check() {
  printf '\n%b==>%b env-файлы\n' "$C_B" "$C_END"
  local fail=0
  local all_hosts="ru $(exit_tags)"
  for tag in $all_hosts; do
    local f="${tag}.env"
    if [[ -f "$DEPLOY_ROOT/envs/$f" ]]; then
      printf '  %-12s %b✓%b\n' "$f" "$C_G" "$C_END"
    else
      printf '  %-12s %b✗ нет%b  (запусти ./setup.sh)\n' "$f" "$C_R" "$C_END"; fail=1
    fi
  done
  [[ $fail -eq 0 ]] || die "не хватает env-файлов — запусти ./setup.sh"

  printf '\n%b==>%b доступность SSH (root@<host>)\n' "$C_B" "$C_END"
  for host in $all_hosts; do
    load_env "$host"
    if [[ -z "${SSH_PASS:-}" ]]; then
      printf '  %-28s %b✗ SSH_PASS пустой — перезапусти ./setup.sh%b\n' \
        "${SSH_USER}@${SSH_HOST}" "$C_R" "$C_END"; fail=1; continue
    fi
    if ssh_exec 'echo ok' >/dev/null 2>&1; then
      printf '  %-28s %b✓%b\n' "${SSH_USER}@${SSH_HOST}" "$C_G" "$C_END"
    else
      printf '  %-28s %b✗ не пускает (IP/пароль/PasswordAuthentication?)%b\n' \
        "${SSH_USER}@${SSH_HOST}" "$C_R" "$C_END"; fail=1
    fi
  done
  [[ $fail -eq 0 ]] || die "SSH-проверка не пройдена — исправь пункты со ✗"
  printf '\n'
}

# ----------------------------------------------------------------------------
# Pre-stage hook for 20-ru-router: pull Hysteria2 secrets from US/SE.
# ----------------------------------------------------------------------------
prep_ru_router() {
  log "prep: тяну секреты Hysteria2 + clash-api с экзит-нод"
  local sec="$DEPLOY_ROOT/secrets/foreign-secrets.env"
  mkdir -p "$DEPLOY_ROOT/secrets"
  : > "$sec"

  for host in $(exit_tags); do
    load_env "$host"
    log "забираю секреты с $host"
    local payload; payload=$(ssh_exec '
      echo "DIRECT=$(cat /etc/anysda/hy2-direct.pwd)"
      echo "WARP=$(cat /etc/anysda/hy2-warp.pwd)"
      echo "OBFS=$(cat /etc/anysda/hy2-obfs.pwd)"
      echo "CLASH=$(cat /etc/anysda/clash-secret.txt)"
    ')
    while IFS='=' read -r k v; do
      [[ -n "$k" ]] && printf '%s_%s=%s\n' "$(echo "$host" | tr a-z A-Z)" "$k" "$v" >> "$sec"
    done <<<"$payload"
  done
  chmod 600 "$sec"
  ok "секреты записаны в secrets/foreign-secrets.env"
}

# ----------------------------------------------------------------------------
# Pre-stage hook for 05-mgmt-mesh: orchestrate WG key generation & distribution.
# ----------------------------------------------------------------------------
prep_mgmt_mesh() {
  log "prep: проверяю keypair wgmgmt на каждой ноде, собираю pubkey-и"
  local secrets="$DEPLOY_ROOT/secrets/wg-mesh"
  mkdir -p "$secrets"

  for host in ru $(exit_tags); do
    load_env "$host"
    log "проверяю keypair на $host"
    local pubkey
    pubkey=$(ssh_exec '
      set -e
      umask 077
      mkdir -p /etc/wireguard
      if [ ! -f /etc/wireguard/wgmgmt.privkey ]; then
        wg genkey > /etc/wireguard/wgmgmt.privkey
        chmod 600 /etc/wireguard/wgmgmt.privkey
      fi
      wg pubkey < /etc/wireguard/wgmgmt.privkey
    ')
    echo "$pubkey" > "$secrets/${host}.pubkey"
    log "  $host → $pubkey"
  done

  local peers="$secrets/peers.env"
  {
    echo "# Сгенерировано deploy.sh prep_mgmt_mesh — не редактируй"
    for host in ru $(exit_tags); do
      local tag_upper; tag_upper=$(echo "$host" | tr a-z A-Z)
      echo "PUBKEY_${tag_upper}=$(cat "$secrets/${host}.pubkey")"
    done
    for host in ru $(exit_tags); do
      local tag_upper; tag_upper=$(echo "$host" | tr a-z A-Z)
      # shellcheck disable=SC1091
      source "$DEPLOY_ROOT/envs/${host}.env"
      echo "EP_${tag_upper}=${SSH_HOST}:${MGMT_PORT:-51900}"
    done
    printf 'EXIT_TAGS="%s"\n' "$(exit_tags)"
  } > "$peers"
  ok "peers.env записан в secrets/wg-mesh/peers.env"
}

# ----------------------------------------------------------------------------
# Run a stage on one host
# ----------------------------------------------------------------------------
run_stage_on_host() {
  local stage="$1" host="$2"

  local script="$DEPLOY_ROOT/scripts/${stage}.sh"
  [[ -f "$script" ]] || die "no such stage: $stage (expected at $script)"

  load_env "$host"
  log "[$stage → $host] starting"

  # Ship a self-contained env file alongside the stage script.
  # SSH_PASS — локальный секрет оркестратора, не должен покидать машину деплоя.
  local rendered_env="$DEPLOY_ROOT/secrets/rendered/${host}.env"
  mkdir -p "$(dirname "$rendered_env")"
  chmod 700 "$(dirname "$rendered_env")" 2>/dev/null || true
  {
    cat "$DEPLOY_ROOT/envs/all.env"
    echo
    grep -v '^SSH_PASS=' "$DEPLOY_ROOT/envs/${host}.env"
  } > "$rendered_env"
  chmod 600 "$rendered_env"

  push "$rendered_env" "${host}.env"
  push "$script"

  # Перед 00-bootstrap пушим публичные ключи оркестратора.
  # Если ~/.ssh/id_ed25519 ещё не существует — генерим keypair (нужен чтобы
  # ssh с оркестратора на удалённые ноды работал по ключу после того, как
  # стейдж отключит парольный вход).
  # В orchestrator_keys пушим ВСЕ доступные pubkey'ы — обычно это один ключ
  # самой машины-оркестратора + (опционально) authorized_keys, если юзер
  # положил туда свой Mac-pubkey для удалённого доступа.
  if [[ "$stage" == "00-bootstrap" ]]; then
    if [[ ! -f "${HOME}/.ssh/id_ed25519" ]]; then
      log "[$stage → $host] keypair оркестратора отсутствует — генерю ed25519"
      mkdir -p "${HOME}/.ssh" && chmod 700 "${HOME}/.ssh"
      ssh-keygen -t ed25519 -N '' -f "${HOME}/.ssh/id_ed25519" -q
      # сразу кладём в свой же authorized_keys чтобы loopback ssh работал
      cat "${HOME}/.ssh/id_ed25519.pub" >> "${HOME}/.ssh/authorized_keys"
      chmod 600 "${HOME}/.ssh/authorized_keys"
    fi

    local combined="$DEPLOY_ROOT/secrets/rendered/orchestrator_keys"
    mkdir -p "$(dirname "$combined")"
    : > "$combined"
    for src in "${HOME}/.ssh/id_ed25519.pub" "${HOME}/.ssh/id_rsa.pub" "${HOME}/.ssh/authorized_keys"; do
      [[ -f "$src" ]] && cat "$src" >> "$combined"
    done
    # uniq по строкам
    awk 'NF && !seen[$0]++' "$combined" > "${combined}.uniq" && mv "${combined}.uniq" "$combined"
    chmod 600 "$combined"

    if [[ -s "$combined" ]]; then
      log "[$stage → $host] раскатываю $(wc -l < "$combined") ключ(а/ей)"
      push "$combined" "orchestrator_keys"
    fi
  fi

  if [[ "$stage" == "05-mgmt-mesh" ]]; then
    local peers="$DEPLOY_ROOT/secrets/wg-mesh/peers.env"
    [[ -f "$peers" ]] || die "secrets/wg-mesh/peers.env не найден — prep_mgmt_mesh не запускался?"
    push "$peers" "peers.env"
  fi

  if [[ "$stage" == "10-foreign" ]]; then
    local tpl="$DEPLOY_ROOT/configs/sing-box-server.json.tpl"
    [[ -f "$tpl" ]] || die "configs/sing-box-server.json.tpl не найден"
    push "$tpl" "sing-box-server.json.tpl"
  fi

  if [[ "$stage" == "20-ru-router" ]]; then
    local sec="$DEPLOY_ROOT/secrets/foreign-secrets.env"
    [[ -f "$sec" ]] || die "secrets/foreign-secrets.env не найден — prep_ru_router не запускался?"
    local gen="$DEPLOY_ROOT/lib/gen-router-config.py"
    [[ -f "$gen" ]] || die "lib/gen-router-config.py не найден"
    push "$sec" "foreign-secrets.env"
    push "$gen" "gen-router-config.py"
  fi

  # Panel + tgbot images live in the public GitLab Container Registry now —
  # 30-frontend / 35-telegram do `docker pull` directly on the host. The only
  # thing 30-frontend still needs from the orchestrator side is the rails
  # template (anysda-config.yaml.tpl).
  if [[ "$stage" == "30-frontend" ]]; then
    push "$DEPLOY_ROOT/configs/anysda-config.yaml.tpl" "anysda-config.yaml.tpl"
  fi

  ssh_exec "chmod +x /tmp/anysda/$(basename "$script") && /tmp/anysda/$(basename "$script") /tmp/anysda/${host}.env"

  ok "[$stage → $host] done"
}

# ----------------------------------------------------------------------------
# Run a stage across a host group, with timing
# ----------------------------------------------------------------------------
run_stage() {
  local stage="$1" group="$2"
  local t0; t0=$(date +%s)

  case "$stage" in
    05-mgmt-mesh) prep_mgmt_mesh ;;
    20-ru-router) prep_ru_router ;;
  esac

  local hosts; hosts=$(expand_hosts "$group" 2>/dev/null)
  for h in $hosts; do
    run_stage_on_host "$stage" "$h"
  done

  local elapsed=$(( $(date +%s) - t0 ))
  local mins=$(( elapsed / 60 )) secs=$(( elapsed % 60 ))
  local t="${mins}m ${secs}s"; [[ "$mins" -eq 0 ]] && t="${secs}s"
  _STAGE_LOG+=("$(printf '%-18s  %-10s  %6s' "$stage" "$group" "$t")")
}

# ----------------------------------------------------------------------------
# Pre-flight: проверка SSH на всех нодах ДО запуска любых стейджей.
# Хостеры иногда geo-блокируют SSH-баннер из RU направления (TCP открыт,
# sshd не отвечает) — это не наша проблема, но без preflight деплой
# падает где-нибудь в середине 00-bootstrap. Лучше сразу сказать.
# ----------------------------------------------------------------------------
preflight_ssh() {
  printf '%b==>%b pre-flight: доступность SSH\n' "$C_B" "$C_END"
  local hosts; hosts=$(expand_hosts all)
  local unreachable=()
  for h in $hosts; do
    load_env "$h"
    if ssh_exec 'echo ok' >/dev/null 2>&1; then
      printf '  %-28s %b✓%b\n' "${SSH_USER}@${SSH_HOST} ($h)" "$C_G" "$C_END"
    else
      printf '  %-28s %b✗%b\n' "${SSH_USER}@${SSH_HOST} ($h)" "$C_R" "$C_END"
      unreachable+=("$h ($SSH_HOST)")
    fi
  done
  if [[ ${#unreachable[@]} -gt 0 ]]; then
    printf '\n%b✗ Недоступны:%b %s\n' "$C_R" "$C_END" "${unreachable[*]}"
    printf '  Что проверить:\n'
    printf '    1. пароль/IP в config.yaml\n'
    printf '    2. PasswordAuthentication=yes в /etc/ssh/sshd_config на ноде\n'
    printf '    3. провайдер не geo-блокирует SSH с RU (часто бывает; TCP открыт,\n'
    printf '       но банер не приходит — замени сервер или попроси разблок)\n'
    die "pre-flight failed — деплой не запускался"
  fi
  printf '\n'
}

# ----------------------------------------------------------------------------
# Post-stage 20: проверка Hysteria2 direct туннелей через clash API.
# Если порт фильтруется на маршруте RU→exit (UDP:443 часто режут), перебираем
# кандидаты, обновляем config.yaml + envs, передеплоиваем 10-foreign на этом
# exit и 20-ru-router. Все кандидаты исчерпаны — die с понятным сообщением.
# ----------------------------------------------------------------------------
HY2_PORT_CANDIDATES=(443 4443 4444 8443 8444 21345)

# Печатает delay (ms) и возвращает 0 если туннель ответил, 1 при таймауте/ошибке.
_clash_delay() {
  local tag="$1"
  load_env ru
  local secret resp d
  secret=$(ssh_exec 'cat /etc/anysda/clash-secret.txt 2>/dev/null') || return 1
  [[ -z "$secret" ]] && return 1
  resp=$(ssh_exec "curl -sS -m 8 -H 'Authorization: Bearer $secret' 'http://10.99.0.1:9090/proxies/hy2-${tag}-direct/delay?timeout=5000&url=http://cp.cloudflare.com/generate_204' 2>/dev/null") || return 1
  d=$(printf '%s' "$resp" | grep -oE '"delay":[0-9]+' | head -1 | grep -oE '[0-9]+' || true)
  if [[ "$d" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s' "$d"
    return 0
  fi
  return 1
}

# Текущий per-exit порт (из all.env: {TAG}_HY2_DIRECT_PORT).
_current_port() {
  local tag="$1"
  local upper
  upper=$(echo "$tag" | tr a-z A-Z)
  # shellcheck disable=SC1091
  ( source "$DEPLOY_ROOT/envs/all.env"; eval "echo \${${upper}_HY2_DIRECT_PORT:-${HY2_DIRECT_PORT:-443}}" )
}

verify_and_rotate_ports() {
  printf '%b==>%b verifying Hysteria2 direct tunnels via clash API\n' "$C_B" "$C_END"
  local t0; t0=$(date +%s)
  local exits; exits=$(exit_tags)
  local rotated_any=0

  for tag in $exits; do
    local delay current
    current=$(_current_port "$tag")
    if delay=$(_clash_delay "$tag"); then
      printf '  %-16s port=%-5s %bRTT=%sms ✓%b\n' "hy2-${tag}-direct" "$current" "$C_G" "$delay" "$C_END"
      continue
    fi
    printf '  %-16s port=%-5s %b× — провайдер фильтрует, перебираю порты%b\n' "hy2-${tag}-direct" "$current" "$C_Y" "$C_END"

    local found=0 port
    for port in "${HY2_PORT_CANDIDATES[@]}"; do
      [[ "$port" == "$current" ]] && continue
      printf '    → пробую :%s ' "$port"
      python3 "$DEPLOY_ROOT/lib/config-set-port.py" "$DEPLOY_ROOT/.." "$tag" "$port" >/dev/null || true
      python3 "$DEPLOY_ROOT/lib/config2env.py"     "$DEPLOY_ROOT/.." >/dev/null || true
      # 10-foreign сам открывает UFW для HY2_DIRECT_PORT (с фикса bug-17), так
      # что bootstrap ре-deploy не нужен — он бы снёс перебинд node_exporter
      # на mgmt IP из стейджа 05.
      run_stage_on_host 10-foreign    "$tag" >/dev/null 2>&1 || true   # sing-box на новом порту
      run_stage_on_host 20-ru-router  ru     >/dev/null 2>&1 || true   # RU роутер пересобрать с новым портом
      sleep 4
      if delay=$(_clash_delay "$tag"); then
        printf '%bRTT=%sms ✓%b\n' "$C_G" "$delay" "$C_END"
        found=1
        rotated_any=1
        break
      fi
      printf '%b×%b\n' "$C_R" "$C_END"
    done

    if [[ $found -eq 0 ]]; then
      printf '\n%b✗ Не подобрался ни один рабочий UDP-порт для %s%b\n' "$C_R" "$tag" "$C_END"
      printf '  Кандидаты: %s\n' "${HY2_PORT_CANDIDATES[*]}"
      printf '  Хостер режет UDP-трафик с RU направления — замени сервер.\n'
      die "verify_and_rotate_ports failed for $tag"
    fi
  done

  local elapsed=$(( $(date +%s) - t0 ))
  local secs=$elapsed mins=0
  if [[ $secs -ge 60 ]]; then mins=$((secs/60)); secs=$((secs%60)); fi
  local t="${secs}s"; [[ $mins -gt 0 ]] && t="${mins}m ${secs}s"
  if [[ $rotated_any -eq 1 ]]; then
    _STAGE_LOG+=("$(printf '%-18s  %-10s  %6s' '[port-rotate]' 'foreign' "$t")")
  fi
  printf '\n'
}

# ----------------------------------------------------------------------------
# Full ordered pipeline
# ----------------------------------------------------------------------------
do_all() {
  print_banner
  _TOTAL_T0=$(date +%s)
  _STAGE_LOG=()

  install_prereqs
  do_check
  preflight_ssh

  run_stage 00-bootstrap     all
  run_stage 05-mgmt-mesh     all
  run_stage 10-foreign       foreign
  run_stage 27-shadowsocks   ru
  run_stage 20-ru-router     ru
  verify_and_rotate_ports
  run_stage 22-adguard       ru
  run_stage 25-monitoring    ru
  run_stage 35-telegram      ru
  run_stage 30-frontend      ru
  run_stage 99-verify        all

  print_summary
  announce_deploy_done
}

# ----------------------------------------------------------------------------
# Поздравительный пинг боту: после успешного pipeline просим бота отправить
# "Установка завершена" + текущие метрики в чат. Если стейдж 35-telegram
# был пропущен (TELEGRAM_BOT_TOKEN не задан) — no-op.
# ----------------------------------------------------------------------------
announce_deploy_done() {
  # shellcheck disable=SC1091
  source "$DEPLOY_ROOT/envs/ru.env" 2>/dev/null || return 0
  [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]] || return 0
  ssh_exec "[ -f /etc/anysda/tgbot-secret.txt ] && curl -fsS --max-time 5 \
      -H 'Content-Type: application/json' \
      -H \"X-Tgbot-Secret: \$(cat /etc/anysda/tgbot-secret.txt)\" \
      -d '{\"type\":\"deploy_done\"}' \
      http://127.0.0.1:8877/event" >/dev/null 2>&1 || true
}

# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
usage() {
  cat <<EOF
anysda-vpn — деплой

  ./deploy.sh                   полный pipeline: prereqs → проверки → деплой
  ./deploy.sh <stage> <group>   запустить одну стадию на группу нод

Стадии:   00-bootstrap | 05-mgmt-mesh | 10-foreign | 20-ru-router |
          22-adguard   | 25-monitoring | 35-telegram | 30-frontend | 99-verify
Группы:   ru | foreign | all | <конкретный тег экзита>
EOF
  exit 0
}

# Читает список тегов выходных нод из envs/exits.env
exit_tags() {
  local exits_file="$DEPLOY_ROOT/envs/exits.env"
  [[ -f "$exits_file" ]] || die "envs/exits.env не найден — запусти ./setup.sh"
  # shellcheck disable=SC1090
  source "$exits_file"
  echo "${EXIT_TAGS:-}"
}

# BASH_SOURCE_ONLY=1 — загружает функции без выполнения CLI-логики.
# Полезно для тестов: source deploy.sh; verify_and_rotate_ports
[[ -n "${BASH_SOURCE_ONLY:-}" ]] && return 0

# Без аргументов → полный pipeline.
# С двумя аргументами → одна стадия на группу.
# -h / --help → подсказка.
case "${1:-}" in
  '')        do_all ;;
  -h|--help) usage ;;
  *)
    [[ $# -lt 2 ]] && die "использование: ./deploy.sh <stage> <group>  (пример: ./deploy.sh 30-frontend ru)"
    run_stage "$1" "$2"
    ;;
esac
