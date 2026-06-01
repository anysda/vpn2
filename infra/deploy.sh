#!/usr/bin/env bash
# anysda-vpn2 — оркестратор деплоя
#
# Использование:
#   ./deploy.sh                   полный pipeline: prereqs → проверки → деплой
#   ./deploy.sh <stage> <group>   одна стадия на группу нод
#
# Стадии:   00-bootstrap | 05-mgmt-mesh | 10-foreign | 20-ru-router |
#           28-wireguard | 29-openvpn | 21-failover-watchdog | 22-adguard
#           25-monitoring | 35-telegram | 30-frontend | 99-verify
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

  # shellcheck disable=SC1091
  local _ru_host _panel_url _agh_url _admin_user _admin_pass
  source "$DEPLOY_ROOT/envs/all.env" 2>/dev/null || true
  source "$DEPLOY_ROOT/envs/ru.env"  2>/dev/null || true
  _ru_host="${SSH_HOST:-${ENTRY_HOST:-}}"
  if [[ -n "${PANEL_DOMAIN:-}" ]]; then
    _panel_url="https://${PANEL_DOMAIN}/"
    _agh_url="https://${PANEL_DOMAIN}:3001/"
  else
    _panel_url="http://${_ru_host}/"
    _agh_url="http://${_ru_host}:3001/"
  fi
  _admin_user="${ADMIN_USER:-admin}"
  # Пароль: из ru.env (если был задан в config.yaml) либо с entry-ноды
  # (автоген в /etc/anysda/admin-password.txt при первом 22-adguard/30-frontend).
  _admin_pass="${ADMIN_PASSWORD:-}"
  if [[ -z "$_admin_pass" ]]; then
    _admin_pass=$(load_env ru 2>/dev/null; ssh_exec 'cat /etc/anysda/admin-password.txt 2>/dev/null' 2>/dev/null) || _admin_pass=""
  fi
  [[ -z "$_admin_pass" ]] && _admin_pass="(см. /etc/anysda/admin-password.txt на entry)"

  # Считаем макс. display-ширину по всем будущим строкам (UTF-8 char count
  # через bash ${#var} в LC_ALL=C.UTF-8). Затем рисуем рамку по этой ширине,
  # каждую строку добиваем пробелами до правого │ — выравнивание гарантировано.
  local _LC_PREV="${LC_ALL:-}"; LC_ALL=C.UTF-8
  local title_str="  деплой завершён  ·  ${timing}  "
  local _rows=("$title_str")
  local entry
  for entry in "${_STAGE_LOG[@]}"; do _rows+=("  ${entry}  "); done
  _rows+=("  Panel:    ${_panel_url}")
  _rows+=("  AdGuard:  ${_agh_url}")
  _rows+=("  Login:    ${_admin_user}")
  _rows+=("  Password: ${_admin_pass}")
  local W=0 r
  for r in "${_rows[@]}"; do (( ${#r} > W )) && W=${#r}; done
  W=$(( W + 12 ))                                 # запас по правому краю для воздуха
  local BAR; BAR=$(printf '─%.0s' $(seq 1 $W))

  _row() {
    local text="$1"
    local pad=$(( W - ${#text} ))
    (( pad < 0 )) && pad=0
    printf '%b  │%b%s%*s%b│%b\n' "$C_G" "$C_END" "$text" "$pad" "" "$C_G" "$C_END"
  }

  printf '\n'
  printf '%b  ┌%s┐%b\n' "$C_G" "$BAR" "$C_END"
  _row "$title_str"
  printf '%b  ├%s┤%b\n' "$C_G" "$BAR" "$C_END"
  for entry in "${_STAGE_LOG[@]}"; do _row "  ${entry}  "; done
  printf '%b  ├%s┤%b\n' "$C_G" "$BAR" "$C_END"
  _row "  Panel:    ${_panel_url}"
  _row "  AdGuard:  ${_agh_url}"
  _row "  Login:    ${_admin_user}"
  _row "  Password: ${_admin_pass}"
  printf '%b  └%s┘%b\n' "$C_G" "$BAR" "$C_END"
  printf '\n'
  LC_ALL="$_LC_PREV"
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
    # Официальное APT-репо Docker'а (signed-by GPG из download.docker.com) —
    # вместо `curl get.docker.com | sh`, который исполняет произвольный
    # shell-код с CDN без проверки. Совпадает с тем, как ставит docker на
    # ноды стадия 25-monitoring. См. SECURITY-AUDIT-2026-06-01.md (H8).
    printf '  ставлю docker (через APT-репо download.docker.com)\n'
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -qq ca-certificates curl gnupg >/dev/null
    install -m 0755 -d /etc/apt/keyrings
    if [[ ! -s /etc/apt/keyrings/docker.gpg ]]; then
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
      chmod a+r /etc/apt/keyrings/docker.gpg
    fi
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
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
  local ssh_tries="${PREFLIGHT_ATTEMPTS:-5}"
  for host in $all_hosts; do
    load_env "$host"
    if [[ -z "${SSH_PASS:-}" ]]; then
      printf '  %-28s %b✗ SSH_PASS пустой — перезапусти ./setup.sh%b\n' \
        "${SSH_USER}@${SSH_HOST}" "$C_R" "$C_END"; fail=1; continue
    fi
    # Ретраим как preflight_ssh — единичный блип не должен валить деплой.
    local ssh_ok=0 i
    for ((i=1; i<=ssh_tries; i++)); do
      if ssh_exec 'echo ok' >/dev/null 2>&1; then ssh_ok=1; break; fi
      [[ $i -lt $ssh_tries ]] && sleep 3
    done
    if [[ $ssh_ok -eq 1 ]]; then
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

    # PEM-сертификат экзита — для TLS-pinning на RU-роутере (см. H5).
    # На старых нодах файл /etc/anysda/hy2-tls.crt отсутствует — пропускаем,
    # тогда gen-router-config.py откатывается на insecure=true (backward-compat).
    mkdir -p "$DEPLOY_ROOT/secrets/exit-certs"
    chmod 700 "$DEPLOY_ROOT/secrets/exit-certs"
    if ssh_exec 'test -f /etc/anysda/hy2-tls.crt'; then
      ssh_exec 'cat /etc/anysda/hy2-tls.crt' > "$DEPLOY_ROOT/secrets/exit-certs/$host.pem"
      chmod 600 "$DEPLOY_ROOT/secrets/exit-certs/$host.pem"
      log "забрал TLS-cert экзита $host"
    else
      warn "на $host нет /etc/anysda/hy2-tls.crt — pin не применён (передеплой 10-foreign)"
    fi
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
# One-time (до параллельного fan-out 00-bootstrap): keypair оркестратора +
# сборка orchestrator_keys. Делается ОДИН раз — иначе при параллельном
# 00-bootstrap несколько хостов гонялись бы за один ключ и config.yaml.
# ----------------------------------------------------------------------------
prep_orchestrator_key() {
  mkdir -p "${HOME}/.ssh" && chmod 700 "${HOME}/.ssh"
  # config2env мог разложить ключ из config.yaml — тогда файл уже на месте.
  if [[ ! -f "${HOME}/.ssh/id_ed25519" ]]; then
    log "keypair оркестратора отсутствует — генерю ed25519"
    ssh-keygen -t ed25519 -N '' -C 'anysda-orchestrator' -f "${HOME}/.ssh/id_ed25519" -q
  fi
  [[ -f "${HOME}/.ssh/id_ed25519.pub" ]] || \
    ssh-keygen -y -f "${HOME}/.ssh/id_ed25519" > "${HOME}/.ssh/id_ed25519.pub"
  cat "${HOME}/.ssh/id_ed25519.pub" >> "${HOME}/.ssh/authorized_keys"
  awk 'NF && !seen[$0]++' "${HOME}/.ssh/authorized_keys" > "${HOME}/.ssh/authorized_keys.u" \
    && mv "${HOME}/.ssh/authorized_keys.u" "${HOME}/.ssh/authorized_keys"
  chmod 600 "${HOME}/.ssh/authorized_keys"
  # Персистим ключ в config.yaml — чтобы пережил пересоздание entry-ноды.
  local _cfg="$DEPLOY_ROOT/../config.yaml"
  if [[ -f "$_cfg" ]] && ! grep -q '^orchestrator_key:' "$_cfg"; then
    log "сохраняю orchestrator-ключ в config.yaml (переживёт пересоздание entry)"
    {
      echo ''
      echo '# orchestrator SSH-ключ — НЕ удалять: нужен для повторного деплоя'
      echo '# с пересозданной entry-ноды. config.yaml надо сохранять/переносить.'
      echo "orchestrator_key: $(base64 -w0 < "${HOME}/.ssh/id_ed25519")"
      echo "orchestrator_pubkey: $(cat "${HOME}/.ssh/id_ed25519.pub")"
    } >> "$_cfg"
  fi
  local combined="$DEPLOY_ROOT/secrets/rendered/orchestrator_keys"
  mkdir -p "$(dirname "$combined")"
  chmod 700 "$(dirname "$combined")" 2>/dev/null || true
  : > "$combined"
  for src in "${HOME}/.ssh/id_ed25519.pub" "${HOME}/.ssh/id_rsa.pub" "${HOME}/.ssh/authorized_keys"; do
    [[ -f "$src" ]] && cat "$src" >> "$combined"
  done
  awk 'NF && !seen[$0]++' "$combined" > "${combined}.uniq" && mv "${combined}.uniq" "$combined"
  chmod 600 "$combined"
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

  # Перед 00-bootstrap раскатываем публичные ключи оркестратора. Сам keypair
  # и файл orchestrator_keys готовит prep_orchestrator_key (однократно).
  if [[ "$stage" == "00-bootstrap" ]]; then
    local combined="$DEPLOY_ROOT/secrets/rendered/orchestrator_keys"
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
    # TLS-pin для Hysteria2 outbound: cert каждого экзита (если уже опубликован
    # обновлённым 10-foreign) → /etc/sing-box/exit-certs/{tag}.pem на RU.
    if [[ -d "$DEPLOY_ROOT/secrets/exit-certs" ]]; then
      push "$DEPLOY_ROOT/secrets/exit-certs" "exit-certs"
    fi
  fi

  if [[ "$stage" == "21-failover-watchdog" ]]; then
    local wd="$DEPLOY_ROOT/lib/failover-watchdog.py"
    [[ -f "$wd" ]] || die "lib/failover-watchdog.py не найден"
    push "$wd" "failover-watchdog.py"
  fi

  # Panel + tgbot images live in the public GitLab Container Registry now —
  # 30-frontend / 35-telegram do `docker pull` directly on the host. The only
  # thing 30-frontend still needs from the orchestrator side is the rails
  # template (anysda-config.yaml.tpl).
  if [[ "$stage" == "30-frontend" ]]; then
    push "$DEPLOY_ROOT/configs/anysda-config.yaml.tpl" "anysda-config.yaml.tpl"
  fi

  # 26-backup кладёт скрипты в /usr/local/bin на entry. Передаём их как
  # вспомогательные файлы, сама стадия install'ит install -m 0755.
  if [[ "$stage" == "26-backup" ]]; then
    for f in anysda-backup.sh anysda-restore.sh anysda-backup-list.sh; do
      local lib="$DEPLOY_ROOT/lib/$f"
      [[ -f "$lib" ]] || die "lib/$f не найден"
      push "$lib" "$f"
    done
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
  [[ "$stage" == "00-bootstrap" ]] && prep_orchestrator_key

  local hosts; hosts=$(expand_hosts "$group" 2>/dev/null)
  # Стадии независимы по нодам — гоняем хосты параллельно. Каждый
  # run_stage_on_host уходит в свой субшелл (из-за `&`), поэтому глобалы
  # load_env (SSH_HOST/SSH_PASS/...) не пересекаются между нодами.
  local pids=() rc=0
  for h in $hosts; do
    run_stage_on_host "$stage" "$h" &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    wait "$pid" || rc=1
  done
  [[ $rc -eq 0 ]] || die "стадия $stage: одна или несколько нод завершились с ошибкой"

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
# TCP :22 reachability probe (no creds needed). Used both for the reference
# endpoints and as a quick liveness check. Returns 0 if a banner-bearing
# socket opens within the timeout.
_tcp22_open() {
  local hostport="$1"
  timeout 8 bash -c "exec 3<>/dev/tcp/${hostport}" 2>/dev/null
}

preflight_ssh() {
  printf '%b==>%b pre-flight: доступность SSH\n' "$C_B" "$C_END"
  local attempts="${PREFLIGHT_ATTEMPTS:-5}"

  # ── Per-node SSH check, с ретраями (терпим transient-сбои) ────────────────
  local hosts; hosts=$(expand_hosts all)
  local unreachable=()
  for h in $hosts; do
    load_env "$h"
    local ok=0 try
    for ((try=1; try<=attempts; try++)); do
      if ssh_exec 'echo ok' >/dev/null 2>&1; then ok=1; break; fi
      sleep 2
    done
    if [[ $ok -eq 1 ]]; then
      printf '  %-30s %b✓%b\n' "${SSH_USER}@${SSH_HOST} ($h)" "$C_G" "$C_END"
    else
      printf '  %-30s %b✗ (%s попыток)%b\n' "${SSH_USER}@${SSH_HOST} ($h)" "$C_R" "$attempts" "$C_END"
      unreachable+=("$h ($SSH_HOST)")
    fi
  done

  # Все ноды доступны — эталон не нужен, идём дальше.
  if [[ ${#unreachable[@]} -eq 0 ]]; then
    printf '\n'
    return 0
  fi

  # ── Часть нод не пускает — опрашиваем эталонный :22 ТОЛЬКО для диагноза ────
  # Эталон github/gitlab — диагностический фактор, НЕ блокер. Сюда мы
  # попадаем только когда ноды уже не пустили; эталон лишь помогает понять,
  # режет ли провайдер центральной ноды исходящий :22, или дело в самих
  # нодах. Если github/gitlab заблокированы РКН, но ноды живые — этот код
  # вообще не выполняется (вышли по return выше).
  local ref_ok=0 ref i
  for ((i=1; i<=3; i++)); do
    for ref in github.com gitlab.com; do
      if _tcp22_open "$ref/22"; then ref_ok=1; break 2; fi
    done
    sleep 2
  done

  printf '\n%b✗ Недоступны по SSH после %s попыток:%b %s\n' \
    "$C_R" "$attempts" "$C_END" "${unreachable[*]}"
  if [[ $ref_ok -eq 0 ]]; then
    printf '  Эталонные github.com:22 / gitlab.com:22 тоже недоступны.\n'
    printf '  %bВероятно%b провайдер центральной ноды режет исходящий :22\n' "$C_Y" "$C_END"
    printf '  (анти-абуз-фильтр). Решения:\n'
    printf '    • тикет хостеру: «разблокируйте исходящий TCP-порт 22»\n'
    printf '    • либо смени центральную ноду на провайдера без фильтра :22\n'
    printf '  Если github/gitlab у тебя просто заблокированы — смотри пункты ниже.\n'
  else
    printf '  Исходящий :22 с центральной ноды работает (эталон OK) — дело в нодах:\n'
  fi
  printf '    1. пароль/IP в config.yaml\n'
  printf '    2. PasswordAuthentication=yes в /etc/ssh/sshd_config на ноде\n'
  printf '    3. firewall / geo-блок на стороне провайдера ноды\n'
  die "pre-flight failed — деплой не запускался"
}

# ----------------------------------------------------------------------------
# Post-stage 20: проверка Hysteria2 direct туннелей через clash API.
# Если порт фильтруется на маршруте RU→exit (UDP:443 часто режут), перебираем
# кандидаты, обновляем config.yaml + envs, передеплоиваем 10-foreign на этом
# exit и 20-ru-router. Все кандидаты исчерпаны — die с понятным сообщением.
# ----------------------------------------------------------------------------
HY2_PORT_CANDIDATES=(443 4443 4444 8443 8444 21345)

# Печатает delay (ms) и возвращает 0 если туннель ответил, 1 при таймауте/ошибке.
# kind: direct | warp — какой outbound экзита проверять.
_clash_delay() {
  local tag="$1" kind="${2:-direct}"
  load_env ru
  local secret resp d
  secret=$(ssh_exec 'cat /etc/anysda/clash-secret.txt 2>/dev/null') || return 1
  [[ -z "$secret" ]] && return 1
  resp=$(ssh_exec "curl -sS -m 8 -H 'Authorization: Bearer $secret' 'http://10.99.0.1:9090/proxies/hy2-${tag}-${kind}/delay?timeout=5000&url=http://cp.cloudflare.com/generate_204' 2>/dev/null") || return 1
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
  printf '%b==>%b verify: проверяю tunnels via clash-api + UDP-пробник для мертвецов\n' "$C_B" "$C_END"
  local t0; t0=$(date +%s)
  local exits; exits=$(exit_tags)
  local rotated_any=0
  local fully_dead=()     # tags, у которых ВЕСЬ UDP-путь RU↔exit фильтруется

  for tag in $exits; do
    local delay current
    current=$(_current_port "$tag")
    # 1. Быстрый путь: туннель отвечает на текущем порту → OK
    if delay=$(_clash_delay "$tag" direct); then
      printf '  %-16s port=%-5s %bRTT=%sms ✓%b\n' "hy2-${tag}-direct" "$current" "$C_G" "$delay" "$C_END"
      continue
    fi
    printf '  %-16s port=%-5s %b×%b\n' "hy2-${tag}-direct" "$current" "$C_Y" "$C_END"

    # 2. Активная UDP-проверка: 5 случайных high-портов с python echo. Если
    # ни на одном из них пакет не дошёл — UDP-путь физически отрезан, exit
    # мертвый, исключаем. Если хоть один сработал — UDP проходит, hyt-rotation
    # имеет смысл (хостер режет конкретные порты вроде 443/8443).
    local exit_host
    load_env "$tag"; exit_host="$SSH_HOST"
    local probe_ports=(33445 39847 41001 51234 60123)
    local udp_alive=""
    printf '    UDP-пробник: '
    for p in "${probe_ports[@]}"; do
      if _probe_udp_one_port "$tag" "$exit_host" "$p"; then
        udp_alive="$p"
        break
      fi
    done
    if [[ -z "$udp_alive" ]]; then
      printf '%bвсе 5 портов фильтруются → exit МЁРТВ, исключаю%b\n' "$C_R" "$C_END"
      fully_dead+=("$tag")
      continue
    fi
    printf '%bUDP проходит на :%s%b — пробую hy2 rotation\n' "$C_G" "$udp_alive" "$C_END"

    # 3. UDP-путь жив → перебираем hy2-кандидаты до 2 попыток
    local found=0 port tries=0 max_tries=2
    for port in "${HY2_PORT_CANDIDATES[@]}"; do
      [[ "$port" == "$current" ]] && continue
      tries=$((tries + 1))
      [[ $tries -gt $max_tries ]] && break
      printf '    → пробую hy2 :%s ' "$port"
      python3 "$DEPLOY_ROOT/lib/config-set-port.py" "$DEPLOY_ROOT/.." "$tag" "$port" >/dev/null || true
      python3 "$DEPLOY_ROOT/lib/config2env.py"     "$DEPLOY_ROOT/.." >/dev/null || true
      run_stage_on_host 10-foreign    "$tag" >/dev/null 2>&1 || true
      run_stage_on_host 20-ru-router  ru     >/dev/null 2>&1 || true
      sleep 4
      if delay=$(_clash_delay "$tag" direct); then
        printf '%bRTT=%sms ✓%b\n' "$C_G" "$delay" "$C_END"
        found=1; rotated_any=1
        break
      fi
      printf '%b×%b\n' "$C_R" "$C_END"
    done
    if [[ $found -eq 0 ]]; then
      printf '  %b⚠ hy2 rotation не подобрала рабочий порт для %s (UDP-путь есть, но\n' "$C_Y" "$tag"
      printf '    hy2-кандидаты режутся). Откатываю порт на дефолт, оставляю как direct-dead.%b\n' "$C_END"
      python3 "$DEPLOY_ROOT/lib/config-set-port.py" "$DEPLOY_ROOT/.." "$tag" "${HY2_DIRECT_PORT:-443}" >/dev/null 2>&1 || true
      python3 "$DEPLOY_ROOT/lib/config2env.py"     "$DEPLOY_ROOT/.." >/dev/null 2>&1 || true
    fi
  done

  # Persist excluded exits + регенерим envs + redeploy 20-ru-router чтобы
  # sing-box, vmagent (25-monitoring) и панель (30-frontend) увидели
  # отфильтрованный EXIT_TAGS на последующих стадиях.
  local state_dir="$DEPLOY_ROOT/state"
  local state_file="$state_dir/excluded-exits.txt"
  mkdir -p "$state_dir"
  if [[ ${#fully_dead[@]} -gt 0 ]]; then
    {
      echo "# Экзиты, исключённые verify_and_rotate_ports."
      echo "# config2env.py читает этот файл и убирает их из EXIT_TAGS на каждом"
      echo "# деплое. Чтобы попробовать снова — удалить тег (или весь файл) и"
      echo "# перезапустить ./deploy.sh — verify прогонится заново."
      echo "# Сгенерировано $(date -u +%FT%TZ)."
      for tag in "${fully_dead[@]}"; do echo "$tag"; done
    } > "$state_file"
    chmod 644 "$state_file"
    printf '\n  %b⚠ Полностью недостижимые экзиты исключены из деплоя: %s%b\n' \
      "$C_Y" "${fully_dead[*]}" "$C_END"
    printf '    Записано в %s\n' "$state_file"
    printf '    Регенерирую envs и пересобираю 20-ru-router чтобы\n'
    printf '    стадии 25-monitoring / 30-frontend / 35-telegram\n'
    printf '    не настраивались с мёртвыми нодами.\n'
    python3 "$DEPLOY_ROOT/lib/config2env.py" "$DEPLOY_ROOT/.." >/dev/null
    run_stage_on_host 20-ru-router ru >/dev/null 2>&1 || true
  else
    # Чистим state-файл если все живы — на случай если предыдущий deploy
    # пометил экзит как мёртвый, а сейчас он ожил.
    [[ -f "$state_file" ]] && rm -f "$state_file"
  fi

  # Если ВСЕ экзиты мертвы — система не функциональна.
  local exits_count=0 dead_count=${#fully_dead[@]}
  for _ in $exits; do exits_count=$((exits_count + 1)); done
  if [[ $dead_count -gt 0 ]] && [[ $dead_count -eq $exits_count ]]; then
    die "verify_and_rotate_ports: все экзиты недостижимы по UDP с RU направления"
  fi

  local elapsed=$(( $(date +%s) - t0 ))
  local secs=$elapsed mins=0
  if [[ $secs -ge 60 ]]; then mins=$((secs/60)); secs=$((secs%60)); fi
  local t="${secs}s"; [[ $mins -gt 0 ]] && t="${mins}m ${secs}s"
  if [[ $rotated_any -eq 1 ]]; then
    _STAGE_LOG+=("$(printf '%-18s  %-10s  %6s' '[port-rotate]' 'foreign' "$t")")
  fi
  if [[ ${#fully_dead[@]} -gt 0 ]]; then
    _STAGE_LOG+=("$(printf '%-18s  %-10s  %6s' '[excluded]' "${fully_dead[*]}" "$t")")
  fi
  printf '\n'
}

# ----------------------------------------------------------------------------
# UDP-пробник между RU entry и exit на одном порту. Используется внутри
# verify_and_rotate_ports как ground-truth: "UDP-путь физически существует
# или нет". Не зависит от sing-box / hy2 / warp — обычный python echo.
# Возвращает 0 если пакет проехал туда-обратно, 1 иначе.
# ----------------------------------------------------------------------------
_probe_udp_one_port() {
  local exit_tag="$1" exit_host="$2" port="$3"
  # 1. На exit: открыть UFW, запустить python UDP echo с таймаутом
  load_env "$exit_tag"
  ssh_exec "
    ufw allow ${port}/udp >/dev/null 2>&1 || true
    nohup python3 -c 'import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(8)
s.bind((\"\", ${port}))
try:
    data, addr = s.recvfrom(1024)
    s.sendto(b\"PONG-\" + data, addr)
except Exception:
    pass
' </dev/null >/dev/null 2>&1 &
    sleep 1
  " >/dev/null 2>&1 || return 1

  # 2. С RU entry: отправить UDP-пробу, ждать echo
  load_env "ru"
  local result
  result=$(ssh_exec "
    timeout 5 python3 -c 'import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(4)
try:
    s.sendto(b\"PING-${port}\", (\"${exit_host}\", ${port}))
    data, _ = s.recvfrom(1024)
    sys.stdout.write(data.decode())
except Exception:
    sys.stdout.write(\"TIMEOUT\")
' 2>/dev/null
  " 2>/dev/null) || true

  # 3. Cleanup: убрать UFW-правило
  load_env "$exit_tag"
  ssh_exec "ufw delete allow ${port}/udp >/dev/null 2>&1 || true" >/dev/null 2>&1 || true

  [[ "$result" == "PONG-PING-${port}" ]]
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
  run_stage 28-wireguard     ru
  run_stage 29-openvpn       ru
  run_stage 20-ru-router     ru
  verify_and_rotate_ports
  run_stage 21-failover-watchdog ru
  run_stage 22-adguard       ru
  run_stage 25-monitoring    ru
  run_stage 26-backup        ru
  run_stage 27-ikev2         ru
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
anysda-vpn2 — деплой

  ./deploy.sh                              полный pipeline: prereqs → проверки → деплой
  ./deploy.sh <stage> <group>              запустить одну стадию на группу нод
  ./deploy.sh backup                       снять зашифрованный backup сейчас
  ./deploy.sh restore [<name>|latest] [--force]
                                           восстановить из backup (default: latest)
  ./deploy.sh backup-list                  список доступных backup'ов

Стадии:   00-bootstrap | 05-mgmt-mesh | 10-foreign | 28-wireguard |
          29-openvpn | 20-ru-router | 21-failover-watchdog | 22-adguard |
          25-monitoring | 26-backup | 35-telegram | 30-frontend | 99-verify
Группы:   ru | foreign | all | <конкретный тег экзита>
EOF
  exit 0
}

# ----------------------------------------------------------------------------
# backup/restore/backup-list — выполняются на entry через ssh_exec.
# ----------------------------------------------------------------------------
do_backup() {
  load_env ru
  ssh_exec '/usr/local/bin/anysda-backup.sh'
}

do_backup_list() {
  load_env ru
  ssh_exec '/usr/local/bin/anysda-backup-list.sh'
}

do_restore() {
  local archive="${1:-latest}"
  local force_flag="${2:-}"
  load_env ru
  # Если архив — локальный путь на оркестраторе, scp'им на entry.
  # Иначе (имя файла или 'latest') — entry резолвит сам.
  if [[ "$archive" != "latest" && -f "$archive" ]]; then
    local base; base=$(basename "$archive")
    log "scp локального архива на entry: $base"
    ssh_exec "mkdir -p \${BACKUP_LOCAL_DIR:-/var/backups/anysda-vpn2} 2>/dev/null || mkdir -p /var/backups/anysda-vpn2"
    push "$archive" "$base"
    ssh_exec "mv /tmp/anysda/'$base' '/var/backups/anysda-vpn2/$base' && chmod 600 '/var/backups/anysda-vpn2/$base'"
    archive="$base"
  fi
  if [[ -n "$force_flag" ]]; then
    ssh_exec "/usr/local/bin/anysda-restore.sh '$archive' '$force_flag'"
  else
    ssh_exec "/usr/local/bin/anysda-restore.sh '$archive'"
  fi
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
  '')          do_all ;;
  -h|--help)   usage ;;
  backup)      do_backup ;;
  backup-list) do_backup_list ;;
  restore)     do_restore "${2:-latest}" "${3:-}" ;;
  *)
    [[ $# -lt 2 ]] && die "использование: ./deploy.sh <stage> <group>  (пример: ./deploy.sh 30-frontend ru)"
    run_stage "$1" "$2"
    ;;
esac
