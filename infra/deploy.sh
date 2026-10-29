#!/usr/bin/env bash
# anysda-vpn2 — оркестратор деплоя
#
# Использование:
#   ./deploy.sh                   полный pipeline: prereqs → проверки → деплой
#   ./deploy.sh <stage> <group>   одна стадия на группу нод
#
# Стадии:   00-bootstrap | 05-mgmt-mesh | 10-foreign | 19-yt-zapret |
#           20-ru-router | 28-wireguard | 29-openvpn | 21-failover-watchdog |
#           22-adguard | 23-emergency | 25-monitoring | 26-backup | 27-ikev2 |
#           35-telegram | 30-frontend | 99-verify
# Группы:   ru | foreign | all | <конкретный тег экзита>

set -euo pipefail

DEPLOY_ROOT="$(cd "$(dirname "$0")" && pwd)"
export DEPLOY_ROOT

# shellcheck source=lib/ssh.sh
source "$DEPLOY_ROOT/lib/ssh.sh"

# Accumulated stage log for the final summary (populated by run_stage):
# «стадия<TAB>группа<TAB>время», колонки выравнивает print_summary
_STAGE_LOG=()
_VERIFY_WARNINGS=()
SKIPPED_EXIT_TAGS=''
_TOTAL_T0=0

exit_is_skipped() {
  grep -qw -- "$1" <<<"${SKIPPED_EXIT_TAGS:-}"
}

active_exit_tags() {
  local tag active=()
  for tag in $(exit_tags); do
    exit_is_skipped "$tag" || active+=("$tag")
  done
  printf '%s\n' "${active[*]}"
}

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
  local _ru_host _panel_url _admin_user _admin_pass
  source "$DEPLOY_ROOT/envs/all.env" 2>/dev/null || true
  source "$DEPLOY_ROOT/envs/ru.env"  2>/dev/null || true
  _ru_host="${SSH_HOST:-${ENTRY_HOST:-}}"
  if [[ -n "${PANEL_DOMAIN:-}" ]]; then
    _panel_url="https://${PANEL_DOMAIN}/"
  else
    _panel_url="http://${_ru_host}/"
    # Без домена HTTPS по IP включается после первого сертификата.
    if [[ "$(ssh_exec 'cat /etc/vpn2/tls/panel/secure' 2>/dev/null)" == true ]]; then
      _panel_url="https://${ENTRY_HOST:-$_ru_host}/"
    fi
  fi
  _admin_user="${ADMIN_USER:-admin}"
  # Сам пароль в рамку не выводим: она остаётся в логах деплоя и в истории
  # терминала. Заданный в config.yaml человек ввёл сам в ./setup.sh, а
  # сгенерированный (22-adguard/30-frontend) лежит на entry, куда у него есть
  # root-доступ по ssh, — печатаем, откуда его взять.
  if [[ -n "${ADMIN_PASSWORD:-}" ]]; then
    _admin_pass="задан в config.yaml (admin.password)"
  else
    _admin_pass="ssh ${SSH_USER:-root}@${_ru_host} cat /etc/anysda/admin-password.txt"
  fi

  # Считаем макс. display-ширину по всем будущим строкам (UTF-8 char count
  # через bash ${#var} в LC_ALL=C.UTF-8). Затем рисуем рамку по этой ширине,
  # каждую строку добиваем пробелами до правого │ — выравнивание гарантировано.
  local _LC_PREV="${LC_ALL:-}"; LC_ALL=C.UTF-8
  local title_str="  деплой завершён  ·  ${timing}  "
  if [[ -n "${_VERIFY_WARNINGS[*]:-}" || -n "${SKIPPED_EXIT_TAGS:-}" ]]; then
    title_str="  ⚠ деплой завершён с предупреждениями  ·  ${timing}  "
  fi
  local _rows=("$title_str")
  # Колонки стадий: ширина по самому длинному значению в символах, а не
  # зашитая (21-failover-watchdog не влезал в %-18s и сдвигал свою строку).
  local entry s g t w1=0 w2=0 w3=0 _stages=()
  for entry in "${_STAGE_LOG[@]}"; do
    IFS=$'\t' read -r s g t <<< "$entry"
    (( ${#s} > w1 )) && w1=${#s}
    (( ${#g} > w2 )) && w2=${#g}
    (( ${#t} > w3 )) && w3=${#t}
  done
  for entry in "${_STAGE_LOG[@]}"; do
    IFS=$'\t' read -r s g t <<< "$entry"
    _stages+=("$s$(printf '%*s' $(( w1 - ${#s} + 2 )) '')$g$(printf '%*s' $(( w2 - ${#g} + w3 - ${#t} + 2 )) '')$t")
  done
  for entry in "${_stages[@]}"; do _rows+=("  ${entry}  "); done
  _rows+=("  Panel:    ${_panel_url}")
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
  for entry in "${_stages[@]}"; do _row "  ${entry}  "; done
  printf '%b  ├%s┤%b\n' "$C_G" "$BAR" "$C_END"
  _row "  Panel:    ${_panel_url}"
  _row "  Login:    ${_admin_user}"
  _row "  Password: ${_admin_pass}"
  printf '%b  └%s┘%b\n' "$C_G" "$BAR" "$C_END"
  printf '\n'
  LC_ALL="$_LC_PREV"
  print_ikev2_notice
  print_sso_notice
  if [[ -n "${_VERIFY_WARNINGS[*]:-}" ]]; then
    warn '99-verify: установка завершена, но не все проверки измерены'
    printf '%s\n' "${_VERIFY_WARNINGS[@]}"
  fi
  if [[ -n "${SKIPPED_EXIT_TAGS:-}" ]]; then
    HOST_TAG=deploy warn "pre-flight: экзиты пропущены из-за недоступного SSH: $SKIPPED_EXIT_TAGS"
  fi
}

print_ikev2_notice() {
  (
    load_env ru
    ssh_exec '
      mode=$(cat /etc/anysda/ikev2-mode) || exit 1
      host=$(cat /etc/anysda/ikev2-server-host) || exit 1
      domain=$(cat /etc/anysda/ikev2-domain 2>/dev/null || true)
      printf "IKEv2: mode=%s server-host=%s\n" "$mode" "$host"
      if [ -n "$domain" ] && { [ "$mode" != letsencrypt ] || [ "$host" != "$domain" ]; }; then
        printf "IKEv2: ожидается сертификат Caddy для %s; автоматическая проверка каждые 30 с.\n" "$domain"
      fi
    ' || warn 'IKEv2: не удалось прочитать состояние сервера'
  )
}

# Засев решается на entry: локальное отсутствие секрета не означает, что
# SSO выключен, если там уже сохранены настройки профиля панели.
print_sso_notice() {
  (
    load_env ru
    local secret="${SSO_CLIENT_SECRET:-}"
    if [[ "${SSO_REQUESTED:-${SSO_ENABLED:-false}}" == true && -z "${secret//[[:space:]]/}" ]] \
       && ssh_exec 'test ! -f /etc/anysda/sso-runtime.json' >/dev/null 2>&1; then
      printf '%s\n' 'SSO не включён, потому что не задан секрет. Входите по паролю администратора. Чтобы включить SSO, заполните sso.client_secret в config.yaml и запустите ./deploy.sh 30-frontend ru.'
    fi
  )
}

# ----------------------------------------------------------------------------
# Ожидание блокировок apt/dpkg (unattended-upgrades на свежей Ubuntu) - общие
# apt_prepare/apt_get из lib/anysda-svc.sh: и здесь, и на нодах перед стадией.
# ----------------------------------------------------------------------------
# shellcheck source=infra/lib/anysda-svc.sh
source "$DEPLOY_ROOT/lib/anysda-svc.sh"

# ----------------------------------------------------------------------------
# Оркестратор обычно запускают на самом entry. Свежий entry через минуту после
# загрузки уходит в unattended-upgrade, и install_prereqs ждал его до 15 минут
# раньше, чем 00-bootstrap успевал поставить паузу. Поэтому на entry пауза
# идёт первым делом; на чужом оркестраторе её ставит 00-bootstrap.
# ----------------------------------------------------------------------------
entry_is_local() {
  local host
  # shellcheck disable=SC1091
  host=$(source "$DEPLOY_ROOT/envs/ru.env" 2>/dev/null; echo "${SSH_HOST:-}")
  [[ -n "$host" ]] || return 1
  [[ "$host" == 127.* || "$host" == localhost ]] && return 0
  [[ " $(hostname -I 2>/dev/null) " == *" $host "* ]]
}

pause_entry_auto_updates() {
  [[ -d /run/systemd/system ]] && entry_is_local || return 0
  # install_prereqs тоже может вызвать needrestart до стадии 00.
  nftables_preserve_rules
  printf '%b==>%b автообновления entry на паузу до конца деплоя\n' "$C_B" "$C_END"
  HOST_TAG=ru apt_auto_pause
}

# ----------------------------------------------------------------------------
# Установка локальных пререквизитов (на самом оркестраторе).
# Apt-пакеты + Docker (для pull/run, build больше не нужен).
# Идемпотентна — если всё уже стоит, no-op за секунду.
# ----------------------------------------------------------------------------
install_prereqs() {
  printf '%b==>%b проверяю локальные зависимости\n' "$C_B" "$C_END"
  local missing=()
  for cmd in ssh scp sshpass envsubst curl python3 rsync openssl; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done

  # Docker оркестратору не нужен: образы тянут 30/35 на самом entry, и ставит
  # его там 00-bootstrap одним заходом apt с остальными пакетами (отдельный
  # заход dpkg ~3 с на ядро).
  local pkgs=()
  for cmd in "${missing[@]}"; do
    case "$cmd" in
      ssh|scp) pkgs+=(openssh-client) ;;
      envsubst) pkgs+=(gettext-base) ;;
      *) pkgs+=("$cmd") ;;
    esac
  done
  [[ ${#pkgs[@]} -eq 0 ]] || pkgs+=(ca-certificates)
  [[ ${#pkgs[@]} -eq 0 ]] || _install_prereq_pkgs "${pkgs[@]}"

  for cmd in ssh scp sshpass envsubst curl python3 rsync openssl; do
    printf '  %-10s %b✓%b\n' "$cmd" "$C_G" "$C_END"
  done
}

_install_prereq_pkgs() {
  local pkgs=("$@") missing=()
  for cmd in ssh scp sshpass envsubst curl python3 rsync openssl; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  [[ -f /etc/debian_version ]] \
    || die "не хватает: ${missing[*]}. На не-Debian/Ubuntu поставь руками."
  printf '  ставлю недостающее: %s\n' "${missing[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt_prepare
  # Оркестратор на самом entry: репо docker - до update, тогда 00-bootstrap
  # ставит docker по свежим спискам без второго update.
  if entry_is_local && ! command -v docker >/dev/null 2>&1; then
    docker_apt_source
  fi
  apt_get update -qq
  apt_get install -y -qq "${pkgs[@]}" >/dev/null
}

# ----------------------------------------------------------------------------
# Проверка env-файлов всех нод. die при первой ошибке.
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

  # Доступность SSH проверяет preflight_ssh следом, всех нод сразу.
  printf '\n'
}

# ----------------------------------------------------------------------------
# Pre-stage hook for 20-ru-router: pull Hysteria2 secrets from US/SE.
# ----------------------------------------------------------------------------
# Экзиты с exits[].hy2_insecure: true работают без проверки сертификата.
# Предупреждаем при сборке роутера и в итоге 99-verify.
warn_insecure_exits() {
  local env="$DEPLOY_ROOT/envs/all.env" host upper
  [[ -f "$env" ]] || return 0
  for host in $(exit_tags); do
    upper=$(printf '%s' "$host" | tr 'a-z-' 'A-Z_')
    if grep -qx "${upper}_HY2_INSECURE=1" "$env"; then
      HOST_TAG=deploy warn "$host: exits[].hy2_insecure: true — сертификат экзита не проверяется, подмену экзита на пути entry → экзит не видно"
    fi
  done
  return 0
}

prep_ru_router() {
  log "prep: тяну секреты Hysteria2 + clash-api с экзит-нод"
  local sec="$DEPLOY_ROOT/secrets/foreign-secrets.env"
  # Для временно недоступного экзита нужны секреты прошлого успешного
  # деплоя: конфигурация хаба сохраняет его outbound до следующего запуска.
  local cached=''
  [[ -f "$sec" ]] && cached=$(<"$sec")
  # Права закрываем до записи: сбор может оборваться между экзитами.
  install -d -m 700 "$DEPLOY_ROOT/secrets" || return
  install -m 600 /dev/null "$sec" || return

  for host in $(exit_tags); do
    if exit_is_skipped "$host"; then
      local upper kept kept_count
      upper=$(printf '%s' "$host" | tr 'a-z-' 'A-Z_')
      kept=$(grep -E "^${upper}_(DIRECT|WARP|OBFS|MGMT|CLASH)=" <<<"$cached" || true)
      kept_count=$(grep -c . <<<"$kept" || true)
      [[ $kept_count -eq 5 ]] \
        || die "$host пропущен в pre-flight, но в прошлом деплое нет его 5 секретов; сначала верни SSH и запусти ./deploy.sh"
      printf '%s\n' "$kept" >> "$sec"
      warn "prep: $host пропущен, использую сохранённые секреты"
      continue
    fi
    load_env "$host"
    log "забираю секреты с $host"
    local payload; payload=$(ssh_exec '
      echo "DIRECT=$(cat /etc/anysda/hy2-direct.pwd)"
      echo "WARP=$(cat /etc/anysda/hy2-warp.pwd)"
      echo "OBFS=$(cat /etc/anysda/hy2-obfs.pwd)"
      echo "MGMT=$(cat /etc/anysda/hy2-mgmt.pwd)"
      echo "CLASH=$(cat /etc/anysda/clash-secret.txt)"
    ')

    # PEM-сертификат экзита закрепляется на entry: без него генератор роутера
    # отказывает. Берём тот, что sing-box экзита реально отдаёт; копия
    # /etc/anysda/hy2-tls.crt есть не на всех установках.
    mkdir -p "$DEPLOY_ROOT/secrets/exit-certs"
    chmod 700 "$DEPLOY_ROOT/secrets/exit-certs"
    local pem="$DEPLOY_ROOT/secrets/exit-certs/$host.pem"
    if (umask 077; ssh_exec 'cat /etc/sing-box/tls.crt 2>/dev/null || cat /etc/anysda/hy2-tls.crt' \
          > "$pem.tmp") && [[ -s "$pem.tmp" ]]; then
      mv -f "$pem.tmp" "$pem"
      log "забрал TLS-cert экзита $host"
    else
      rm -f "$pem.tmp" "$pem"
      warn "на $host нет сертификата Hysteria2 — закрепится ранее сохранённый на entry, иначе 20-ru-router откажет (передеплой 10-foreign)"
    fi
    # Хост-ключ экзита для known_hosts entry: ssh <tag> ходит на 127.0.0.1
    # через служебный туннель, сверять ключ по адресу нельзя (у всех один).
    mkdir -p "$DEPLOY_ROOT/secrets/exit-hostkeys"
    ssh_exec 'cat /etc/ssh/ssh_host_ed25519_key.pub' \
      > "$DEPLOY_ROOT/secrets/exit-hostkeys/$host.pub" 2>/dev/null \
      || rm -f "$DEPLOY_ROOT/secrets/exit-hostkeys/$host.pub"
    # Экзит доверен меньше entry, а 20-ru-router читает этот файл от root.
    # Пропускаем только известные ключи со значением в алфавите gen_pwd
    # из 10-foreign; сертификат и хост-ключ должны разбираться как есть.
    if [[ -f "$DEPLOY_ROOT/secrets/exit-certs/$host.pem" ]] \
       && ! openssl x509 -noout -in "$DEPLOY_ROOT/secrets/exit-certs/$host.pem" 2>/dev/null; then
      rm -f "$DEPLOY_ROOT/secrets/exit-certs/$host.pem"
      die "$host отдал не PEM-сертификат Hysteria2"
    fi
    if [[ -f "$DEPLOY_ROOT/secrets/exit-hostkeys/$host.pub" ]] \
       && ! ssh-keygen -lf "$DEPLOY_ROOT/secrets/exit-hostkeys/$host.pub" >/dev/null 2>&1; then
      rm -f "$DEPLOY_ROOT/secrets/exit-hostkeys/$host.pub"
      die "$host отдал не ssh-ключ в /etc/ssh/ssh_host_ed25519_key.pub"
    fi
    local k v seen=0
    while IFS='=' read -r k v; do
      [[ -n "$k" ]] || continue
      case "$k" in DIRECT|WARP|OBFS|MGMT|CLASH) ;; *) die "$host отдал лишний ключ секретов: $k" ;; esac
      [[ "$v" =~ ^[A-Za-z0-9+/]{16,128}$ ]] \
        || die "$host: секрет $k не похож на пароль из 10-foreign (16-128 знаков A-Z a-z 0-9 + /), стадию 20 не запускаю"
      printf '%s_%s=%s\n' "$(echo "$host" | tr 'a-z-' 'A-Z_')" "$k" "$v" >> "$sec"
      seen=$((seen + 1))
    done <<<"$payload"
    [[ $seen -eq 5 ]] || die "$host отдал $seen секретов из 5"
  done
  chmod 600 "$sec"
  ok "секреты записаны в secrets/foreign-secrets.env"
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
# Свой ключ entry для ssh на экзиты через служебный hy2-туннель (стадия 20
# пишет ~/.ssh/config с хостами по тегам). Генерится на самой entry, а не
# на машине деплоя. Публичная часть уходит на экзиты в 00-bootstrap; там она
# заменяет прежний ключ entry по комментарию anysda-entry-mgmt, так что
# пересозданная entry не копит старые ключи.
# ----------------------------------------------------------------------------
ENTRY_MGMT_KEY_COMMENT='anysda-entry-mgmt'

prep_entry_mgmt_key() {
  local out="$DEPLOY_ROOT/secrets/rendered/entry_mgmt_key"
  mkdir -p "$(dirname "$out")"
  rm -f "$out"
  local pub
  pub=$(load_env ru && ssh_exec "
    mkdir -p /root/.ssh && chmod 700 /root/.ssh
    [ -f /root/.ssh/id_ed25519_mgmt ] || ssh-keygen -t ed25519 -N '' -q \
      -C '$ENTRY_MGMT_KEY_COMMENT' -f /root/.ssh/id_ed25519_mgmt
    cat /root/.ssh/id_ed25519_mgmt.pub") || pub=''
  if [[ "$pub" != ssh-ed25519\ *" $ENTRY_MGMT_KEY_COMMENT" ]]; then
    HOST_TAG=deploy warn "ключ entry для ssh на экзиты не получен — на экзитах остаётся прежний"
    return 0
  fi
  printf '%s\n' "$pub" > "$out"
  chmod 600 "$out"
}

# ----------------------------------------------------------------------------
# Run a stage on one host
# ----------------------------------------------------------------------------
# Стадии, которые меняют ufw или свои nft-таблицы. На время такой стадии на
# ноде стоит сторож lib/fw-guard.sh: если новые ssh-входы не проходят дольше
# окна, он сам возвращает фаервол к состоянию до стадии. Таблицы
# inet vpn2_emergency_hy2 (23-emergency) в снимке сторожа нет намеренно: она
# закрывает только UDP-порт аварийки и исходящие vpn2-hy2, ssh перекрыть не
# может, а её откат сломал бы идущий аварийный режим. 99-verify фаервол не меняет.
FW_GUARD_STAGES='00-bootstrap 05-mgmt-mesh 10-foreign 19-yt-zapret 20-ru-router 22-adguard 23-emergency 27-ikev2 28-wireguard 29-openvpn 30-frontend'
FW_GUARD_WINDOW=${VPN2_FW_GUARD_WINDOW:-90}

fw_guard_stage() {
  [[ " $FW_GUARD_STAGES " == *" $1 "* ]]
}

# deploy.sh идёт с самой entry, и её ssh к себе ходит через lo: такой вход
# проходит и тогда, когда стадия закрыла ssh снаружи. Внешний вход проверяет
# экзит: новое ssh-соединение к нему и TCP к ENTRY_HOST:22 оттуда, всё в пределах
# шага пульса.

# Какой адрес entry.host: local - экзит по нему увидит свой же sshd (loopback,
# не задан), private - внутренняя сеть или CGNAT (entry за NAT), иначе public.
fw_guard_entry_addr_kind() {
  local addr=$1
  if [[ -n $addr && ! $addr =~ ^[0-9.]+$ && $addr != *:* ]]; then
    addr=$(getent ahosts "$addr" 2>/dev/null | awk 'NR == 1 { print $1 }') || addr=''
    [[ -n $addr ]] || { [[ $1 == localhost ]] && echo local || echo public; return; }
  fi
  case $addr in
    ''|127.*|0.0.0.0|::|::1) echo local ;;
    10.*|192.168.*|169.254.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) echo private ;;
    100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) echo private ;;
    [fF][cCdD]*:*|[fF][eE]80:*) echo private ;;
    *) echo public ;;
  esac
}

# Видит ли экзит TAG вход на ENTRY_HOST:22. Код: 0 видит, 1 не видит, 3 не ответил.
# Экзит печатает метку сразу после входа, до TCP-пробы: метка пришла, а дальше
# отказ или таймаут сессии - экзит ответил и входа не видит (вход закрыт отбросом,
# медленный вход по ssh съел время пробы). Без метки - не ответил. Потолок сессии
# - медленный вход по ssh (до 10 с) и TCP-проба (3 с) с запасом.
fw_guard_exit_sees_entry() {
  local check rc=0 mark=vpn2-fw-guard-exit-answered
  printf -v check 'echo %s; if timeout 3 bash -c %q 2>/dev/null; then exit 0; fi; exit 1' \
    "$mark" "exec 3<>/dev/tcp/$ENTRY_HOST/22"
  (
    load_env "$1" || exit 9
    code=0
    out=$(SSH_PROBE_TIMEOUT=15 ssh_fresh "$check" 2>/dev/null) || code=$?
    # 1 - ответ команды на экзите; отличаем от die в load_env.
    if (( code == 1 )) || { (( code == 124 || code == 137 )) &&
         [[ $'\n'$out$'\n' == *$'\n'"$mark"$'\n'* ]]; }; then
      code=10
    fi
    exit "$code"
  ) >/dev/null 2>&1 || rc=$?
  case $rc in
    0) return 0 ;;
    10) return 1 ;;
  esac
  return 3
}

# Все экзиты из списка разом, строка «тег код» на каждый: шаг пульса - одна
# сессия, сколько бы экзитов ни молчало до потолка, и окно сторожа не съедается.
fw_guard_exits_see_entry() {
  local tag
  for tag in $1; do
    ( rc=0; fw_guard_exit_sees_entry "$tag" || rc=$?; echo "$tag $rc" ) &
  done
  wait
}

# Опрос всех экзитов из списка, а не первого ответившего: экзит, у которого нет
# пути до entry:22, не перекрывает остальных.
# Код: 0 хоть один видит вход, 1 ответившие не видят, 3 ни один не ответил.
fw_guard_entry_probe() {
  local codes
  codes=$(fw_guard_exits_see_entry "$1")
  [[ $codes$'\n' == *$' 0\n'* ]] && return 0
  [[ $codes$'\n' == *$' 1\n'* ]] && return 1
  return 3
}

# Перед arm стадии на entry, пока фаервол ещё прежний. Опорные экзиты - те, что
# видят вход снаружи сейчас: во время стадии «закрыт» значит, что его не видит ни
# один из них, то есть закрыла стадия, а не путь экзита. Внешней точки нет
# (экзитов нет, они не отвечают, entry.host локальный, entry за NAT) - опорных
# нет, пульс через lo и один warn с причиной. Так же и публичный entry.host,
# который до стадии не видит ни один ответивший экзит (провайдер режет 22
# снаружи, NAT с публичным адресом, у экзитов закрыт исходящий 22): закрыла ли
# вход стадия, отсюда не отличить.
FW_GUARD_ENTRY_REFS=''
fw_guard_entry_baseline() {
  local stage=$1 tags tag rc kind open='' answered=''
  FW_GUARD_ENTRY_REFS=''
  tags=$(active_exit_tags 2>/dev/null) || tags=''
  if [[ -z "${tags// /}" ]]; then
    warn "[fw-guard] вход на entry снаружи не проверяется: экзитов нет"
    return 0
  fi
  kind=$(fw_guard_entry_addr_kind "${ENTRY_HOST:-}")
  if [[ $kind == local ]]; then
    warn "[fw-guard] вход на entry снаружи не проверяется: entry.host '${ENTRY_HOST:-}' локальный, экзит увидел бы свой sshd"
    return 0
  fi
  # Экзиты, ответившие «вижу», - сразу опорные. Ответившие «не вижу» перед
  # выводом «не виден ни одному» пробуются ещё 2 раза через 5 с: одна потерянная
  # TCP-проба не должна оставить стадию без опорных.
  local probe=$tags try codes
  for try in 1 2 3; do
    (( try == 1 )) || sleep 5
    codes=$(fw_guard_exits_see_entry "$probe")
    probe=''
    while read -r tag rc; do
      case $rc in
        0) open+=" $tag" ;;
        1) probe+=" $tag"; [[ " $answered " == *" $tag "* ]] || answered+=" $tag" ;;
      esac
    done <<< "$codes"
    [[ -z $open && -n $probe ]] || break
  done
  if [[ -n $open ]]; then
    FW_GUARD_ENTRY_REFS=${open# }
  elif [[ -z $answered ]]; then
    warn "[fw-guard] вход на entry снаружи не проверяется: экзиты пока не отвечают по ssh"
  elif [[ $kind == private ]]; then
    warn "[fw-guard] вход на entry снаружи не проверяется: entry.host $ENTRY_HOST внутренний и до стадии не виден ни одному экзиту (entry за NAT)"
  else
    warn "[fw-guard] до стадии вход на $ENTRY_HOST:22 снаружи не виден ни одному экзиту (${answered# }) - вход на entry снаружи не проверяется"
  fi
}

# Пульс сторожу: новое соединение каждые 5 с, пока жив deploy.sh. На entry с
# опорными экзитами пульс пропущен, только когда ответившие экзиты не видят
# вход: его закрыла стадия, сторож откатит фаервол по окну. ssh entry→экзит -
# исходящее соединение entry, закрытый вход его не рвёт, поэтому молчание всех
# экзитов (ребут, перезапуск sshd в их стадии) о входе не говорит: пульс через
# lo и один warn. Закрытый вход при молчащих экзитах так не поймать.
fw_guard_beat() {
  local outside gone=0
  while kill -0 $$ 2>/dev/null; do
    outside=0
    if [[ "$HOST_TAG" == ru && -n "${FW_GUARD_ENTRY_REFS:-}" ]]; then
      fw_guard_entry_probe "$FW_GUARD_ENTRY_REFS" || outside=$?
      if (( outside == 3 && ! gone )); then
        warn "[fw-guard] экзиты перестали отвечать по ssh, вход на entry снаружи не проверяется - пульс через lo"
        gone=1
      fi
    fi
    if (( outside != 1 )); then
      ssh_fresh 'bash --norc /run/vpn2-fw-guard/fw-guard.sh beat' >/dev/null 2>&1 || true
    fi
    sleep 5
  done
}

# Снять сторожа тоже новым соединением. Пока оно не проходит, сторож успеет
# откатить фаервол, и тогда disarm ответит кодом 3.
fw_guard_disarm() {
  local stage=$1 deadline rc out waited=0
  # disarm на entry идёт через lo и снял бы сторожа при закрытом входе снаружи.
  # Проба по шагу пульса, пока ответившие опорные экзиты не видят вход: одна
  # потерянная проба не решает. Не видят дольше окна - пульса не было, сторож
  # откатил, и disarm ответит кодом 3. Ни один не ответил (код 3) - о входе
  # это не говорит, как и в пульсе: disarm сразу.
  if [[ "$HOST_TAG" == ru && -n "${FW_GUARD_ENTRY_REFS:-}" ]]; then
    while :; do
      rc=0
      fw_guard_entry_probe "$FW_GUARD_ENTRY_REFS" || rc=$?
      (( rc == 1 )) || break
      if (( waited >= FW_GUARD_WINDOW + 5 )); then
        warn "[$stage → $HOST_TAG] экзиты не видят вход по ssh на entry снаружи ${waited} с, сторож должен был откатить фаервол"
        break
      fi
      sleep 5
      waited=$(( waited + 5 ))
    done
  fi
  deadline=$(( $(date +%s) + FW_GUARD_WINDOW + 30 ))
  while :; do
    rc=0
    out=$(ssh_fresh "bash --norc /run/vpn2-fw-guard/fw-guard.sh disarm $stage" 2>&1) || rc=$?
    case $rc in
      # 4: сторожа нет - стадия упала до arm, и её ошибка уже в rc.
      0|4) return 0 ;;
      3) warn "[$stage → $HOST_TAG] $out"; return 1 ;;
      # Shell узла не дошёл до команды: причина в $out, повтор не поможет.
      "$_SSH_NO_MARK") warn "[$stage → $HOST_TAG] $out"; return 1 ;;
    esac
    if (( $(date +%s) >= deadline )); then
      warn "[$stage → $HOST_TAG] после стадии нода не пускает новые ssh-входы ${FW_GUARD_WINDOW} с и дольше; сторож фаервола должен был откатить ufw, проверь вход руками"
      return 1
    fi
    sleep 5
  done
}

run_stage_on_host() {
  local stage="$1" host="$2"
  # Код 2 зарезервирован для результата 99-verify; ошибки подготовки — 1.

  local script="$DEPLOY_ROOT/scripts/${stage}.sh"
  [[ -f "$script" ]] || die "no such stage: $stage (expected at $script)"

  load_env "$host" || return 1
  log "[$stage → $host] starting"

  # Ship a self-contained env file alongside the stage script.
  # SSH_PASS — локальный секрет оркестратора, не должен покидать машину деплоя.
  local rendered_env="$DEPLOY_ROOT/secrets/rendered/${host}.env"
  mkdir -p "$(dirname "$rendered_env")" || return 1
  chmod 700 "$(dirname "$rendered_env")" || return 1
  {
    cat "$DEPLOY_ROOT/envs/all.env" || return 1
    echo
    grep -v '^SSH_PASS=' "$DEPLOY_ROOT/envs/${host}.env"
    if [[ "${RESTORE_PRESERVE_ADMIN:-0}" == 1 ]]; then
      echo 'unset ADMIN_PASSWORD'
    fi
    printf 'VPN2_DEVELOPMENT=%q\n' "${VPN2_DEVELOPMENT:-false}"
    printf 'VPN2_PANEL_FOLLOWS=%q\n' "${VPN2_PANEL_FOLLOWS:-false}"
    echo 'VPN2_RELEASE_MANIFEST=/tmp/anysda/release-manifest.json'
  } > "$rendered_env" || return 1
  chmod 600 "$rendered_env" || return 1

  push "$rendered_env" "${host}.env" || return 1
  push "$script" || return 1
  # Общие функции стадий (перезапуск службы только при смене её входов).
  push "$DEPLOY_ROOT/lib/anysda-svc.sh" || return 1
  push "$DEPLOY_ROOT/lib/anysda-pki.sh" || return 1
  push "$DEPLOY_ROOT/lib/release.sh" || return 1
  push "$DEPLOY_ROOT/lib/release.py" || return 1
  push "$DEPLOY_ROOT/lib/deploy_state.py" || return 1
  push "$DEPLOY_ROOT/lib/panel-transaction.sh" || return 1
  if [[ -n "${VPN2_RELEASE_MANIFEST:-}" && -f "$VPN2_RELEASE_MANIFEST" ]]; then
    push "$VPN2_RELEASE_MANIFEST" release-manifest.json || return 1
  fi
  push "$DEPLOY_ROOT/lib/ufw-rules.py" || return 1
  local guard=''
  if fw_guard_stage "$stage"; then
    push "$DEPLOY_ROOT/lib/fw-guard.sh" || return 1
    guard="bash --norc /tmp/anysda/fw-guard.sh arm $stage $FW_GUARD_WINDOW || exit 1; "
  fi
  if [[ "$stage" == @(23-emergency|30-frontend) ]]; then
    push "$DEPLOY_ROOT/lib/vpn2_tls.py" || return 1
  fi
  if [[ "$stage" == @(22-adguard|25-monitoring|30-frontend|35-telegram) ]]; then
    push "$DEPLOY_ROOT/lib/entry-resources.sh" || return 1
    push "$DEPLOY_ROOT/lib/entry_resources.py" || return 1
  fi
  push "$DEPLOY_ROOT/../app/shared/vpn-pools.json" || return 1
  if [[ "$stage" == "29-control" ]]; then
    push "$DEPLOY_ROOT/control" "control" || return 1
  fi
  # Контрольный клиент и цель проверки трафика: клиент идёт только на entry.
  if [[ "$stage" == 99-verify && "$host" == ru ]]; then
    push "$DEPLOY_ROOT/lib/verify-traffic.py" || return 1
    push "$DEPLOY_ROOT/lib/verify-target.py" || return 1
  fi

  # Код аварийки приходит образом из реестра (стадия 23), здесь только конфиг узла.
  if [[ "$stage" == '23-emergency' ]]; then
    push "$DEPLOY_ROOT/secrets/emergency/$host.json" emergency.json || return 1
  fi

  # Общая валидация bandwidth; 22 импортирует тот же генератор для AdGuard.
  case "$stage" in
    10-foreign|20-ru-router|22-adguard) push "$DEPLOY_ROOT/lib/hy2_bandwidth.py" || return 1 ;;
  esac

  if [[ "$stage" == "10-foreign" || "$stage" == "20-ru-router" ]]; then
    push "$DEPLOY_ROOT/lib/install-sing-box.sh" || return 1
  fi

  # Перед 00-bootstrap раскатываем публичные ключи оркестратора. Сам keypair
  # и файл orchestrator_keys готовит prep_orchestrator_key (однократно).
  if [[ "$stage" == "00-bootstrap" ]]; then
    local combined="$DEPLOY_ROOT/secrets/rendered/orchestrator_keys"
    if [[ -s "$combined" ]]; then
      log "[$stage → $host] раскатываю $(wc -l < "$combined") ключ(а/ей)"
      push "$combined" "orchestrator_keys" || return 1
    fi
    local entry_key="$DEPLOY_ROOT/secrets/rendered/entry_mgmt_key"
    if [[ "$host" != ru && -s "$entry_key" ]]; then
      push "$entry_key" "entry_mgmt_key" || return 1
    fi
  fi

  if [[ "$stage" == "10-foreign" ]]; then
    local tpl="$DEPLOY_ROOT/configs/sing-box-server.json.tpl"
    [[ -f "$tpl" ]] || die "configs/sing-box-server.json.tpl не найден"
    push "$tpl" "sing-box-server.json.tpl" || return 1
    push "$DEPLOY_ROOT/lib/warp-watchdog.py" "warp-watchdog.py" || return 1
  fi

  if [[ "$stage" == "20-ru-router" ]]; then
    local sec="$DEPLOY_ROOT/secrets/foreign-secrets.env"
    [[ -f "$sec" ]] || die "secrets/foreign-secrets.env не найден — prep_ru_router не запускался?"
    local gen="$DEPLOY_ROOT/lib/gen-router-config.py"
    [[ -f "$gen" ]] || die "lib/gen-router-config.py не найден"
    push "$sec" "foreign-secrets.env" || return 1
    push "$gen" "gen-router-config.py" || return 1
    # TLS-pin для Hysteria2 outbound: cert каждого экзита (если уже опубликован
    # обновлённым 10-foreign) → /etc/sing-box/exit-certs/{tag}.pem на RU.
    if [[ -d "$DEPLOY_ROOT/secrets/exit-certs" ]]; then
      push "$DEPLOY_ROOT/secrets/exit-certs" "exit-certs" || return 1
    fi
    if [[ -d "$DEPLOY_ROOT/secrets/exit-hostkeys" ]]; then
      push "$DEPLOY_ROOT/secrets/exit-hostkeys" "exit-hostkeys" || return 1
    fi
  fi

  # 22-adguard берёт из генератора апстрим YouTube -> российский резолвер:
  # список доменов и адрес резолвера живут только в gen-router-config.py.
  if [[ "$stage" == "22-adguard" ]]; then
    push "$DEPLOY_ROOT/lib/gen-router-config.py" "gen-router-config.py" || return 1
  fi

  if [[ "$stage" == "21-failover-watchdog" ]]; then
    local wd="$DEPLOY_ROOT/lib/failover-watchdog.py"
    [[ -f "$wd" ]] || die "lib/failover-watchdog.py не найден"
    push "$wd" "failover-watchdog.py" || return 1
  fi

  # Panel + tgbot images live in the public GitHub Container Registry (ghcr.io) —
  # 30-frontend / 35-telegram do `docker pull` directly on the host. The only
  # thing 30-frontend still needs from the orchestrator side is the rails
  # template (anysda-config.yaml.tpl).
  if [[ "$stage" == @(30-frontend|35-telegram) ]]; then
    push "$DEPLOY_ROOT/configs/anysda-config.yaml.tpl" "anysda-config.yaml.tpl" || return 1
    if [[ "$stage" == "35-telegram" ]]; then
      push "$DEPLOY_ROOT/scripts/30-frontend.sh" || return 1
    fi
  fi

  # 19-yt-zapret генерит nft-таблицу блокировки QUIC к Google: нужен генератор
  # и снапшот списка префиксов (fallback, если gstatic недоступен при прогоне).
  if [[ "$stage" == "19-yt-zapret" ]]; then
    for f in gen-yt-quic-nft.py google-prefixes.json yt-failover.py install-yt-pot.sh; do
      local lib="$DEPLOY_ROOT/lib/$f"
      [[ -f "$lib" ]] || die "lib/$f не найден"
      push "$lib" "$f" || return 1
    done
  fi

  # 26-backup кладёт скрипты в /usr/local/bin на entry. Передаём их как
  # вспомогательные файлы, сама стадия install'ит install -m 0755.
  if [[ "$stage" == "26-backup" ]]; then
    for f in anysda-backup.sh anysda-restore.sh anysda-backup-list.sh; do
      local lib="$DEPLOY_ROOT/lib/$f"
      [[ -f "$lib" ]] || die "lib/$f не найден"
      push "$lib" "$f" || return 1
    done
  fi

  # Раньше 30-frontend ставил Caddy из APT-репо Cloudsmith, а оно теперь отвечает
  # 402 Payment Required: оставшийся на старой ноде список валит apt-get update
  # первой же стадии, задолго до 30-frontend. Убираем его до любой стадии.
  # --norc: вложенный bash по ssh читает ~/.bashrc узла, а его `[ -z "$PS1" ]` под -u
  # сыплет «PS1: unbound variable» в лог каждой стадии.
  ssh_exec "rm -f /etc/apt/sources.list.d/caddy-stable.list /usr/share/keyrings/caddy-stable-archive-keyring.gpg &&
bash --norc -euo pipefail -c 'HOST_TAG=$stage; source /tmp/anysda/anysda-svc.sh; apt_prepare'" | sed -u "s/^/[$host]/" || return 1
  # Явный return: под `if`/`||` (перекатка порта, ветка мёртвых экзитов) set -e
  # не действует, и упавшая стадия возвращала 0 по последнему `ok`.
  local rc=0 beat=''
  if [[ -n "$guard" ]]; then
    FW_GUARD_ENTRY_REFS=''
    [[ "$HOST_TAG" != ru ]] || fw_guard_entry_baseline "$stage"
    fw_guard_beat &
    beat=$!
  fi
  ssh_exec "chmod +x /tmp/anysda/$(basename "$script") || exit 1; ${guard}/tmp/anysda/$(basename "$script") /tmp/anysda/${host}.env" || rc=$?
  if [[ -n "$beat" ]]; then
    kill "$beat" 2>/dev/null || true
    wait "$beat" 2>/dev/null || true
    fw_guard_disarm "$stage" || rc=1
  fi
  # Экзиту env с адресами entry и соседей нужен только на время стадии:
  # каждая стадия кладёт свои файлы заново. На entry /tmp/anysda не трогаем.
  if [[ "$host" != ru ]]; then
    ssh_exec 'rm -rf /tmp/anysda' || warn "[$stage → $host] не удалось убрать /tmp/anysda"
  fi
  if [[ "$stage" == 99-verify && $rc -eq 2 ]]; then
    warn "[$stage → $host] не все проверки измерены"
    return 2
  fi
  [[ $rc -eq 0 ]] || return 1

  ok "[$stage → $host] done"
}

# ----------------------------------------------------------------------------
# Run a stage across a host group, with timing
# ----------------------------------------------------------------------------
run_stage() {
  local stage="$1" group="$2"
  local t0; t0=$(date +%s)

  case "$stage" in
    20-ru-router) prep_ru_router; warn_insecure_exits ;;
    23-emergency) python3 "$DEPLOY_ROOT/emergency/provision.py" "$DEPLOY_ROOT/.." || return 1 ;;
  esac
  if [[ "$stage" == "00-bootstrap" ]]; then
    prep_orchestrator_key
    prep_entry_mgmt_key
  fi

  local hosts; hosts=$(expand_hosts "$group" 2>/dev/null)
  if [[ "$stage" == 10-foreign ]]; then
    local host count=0
    for host in $(expand_hosts foreign); do count=$((count + 1)); done
    [[ "$count" != 1 ]] || warn 'единственный экзит: на время его обновления возможен перерыв VPN'
    for host in $hosts; do
      run_stage_on_host "$stage" "$host" || return 1
      run_stage_on_host 99-verify "$host" || return 1
    done
    _STAGE_LOG+=("$stage"$'\t'"$group"$'\t'"$(( $(date +%s) - t0 ))s")
    return 0
  fi
  # Стадии независимы по нодам — гоняем хосты параллельно. Каждый
  # run_stage_on_host уходит в свой субшелл (из-за `&`), поэтому глобалы
  # load_env (SSH_HOST/SSH_PASS/...) не пересекаются между нодами.
  local pids=() nodes=() rc=0 incomplete=0 host_rc pid i h line
  local verify_logs=''
  if [[ "$stage" == 99-verify ]]; then
    # Причины нужны print_summary в родительском процессе, а вывод идёт сразу.
    verify_logs=$(mktemp -d)
    local skipped
    for skipped in ${SKIPPED_EXIT_TAGS:-}; do
      case "$group" in
        all|foreign|"$skipped")
          printf '[%s] ⊘ пропущен: SSH недоступен в pre-flight\n' "$skipped"
          incomplete=1
          ;;
      esac
    done
  fi
  for h in $hosts; do
    if [[ -n "$verify_logs" ]]; then
      # Подоболочка: у голого `cmd | tee &` $! — это tee, и bash 5.2
      # отдаёт wait её код 0, если задание уже завершилось.
      ( set -o pipefail; run_stage_on_host "$stage" "$h" 2>&1 | tee "$verify_logs/$h" ) &
    else
      run_stage_on_host "$stage" "$h" &
    fi
    pids+=("$!")
    nodes+=("$h")
  done
  for i in "${!pids[@]}"; do
    pid=${pids[$i]}
    host_rc=0
    wait "$pid" || host_rc=$?
    if [[ "$stage" == 99-verify && $host_rc -eq 2 ]]; then
      incomplete=1
      local found=0
      while IFS= read -r line; do
        if [[ "$line" == '['*'] ? '*'не проверено:'* ]]; then
          _VERIFY_WARNINGS+=("$line")
          found=1
        fi
      done < "$verify_logs/${nodes[$i]}"
      if [[ $found -eq 0 ]]; then
        _VERIFY_WARNINGS+=("[${nodes[$i]}] ? не проверено: 99-verify вернул код 2; подробности выше")
      fi
    elif [[ $host_rc -ne 0 ]]; then
      rc=1
    fi
  done
  [[ -z "$verify_logs" ]] || rm -r -- "$verify_logs"
  # Предупреждение об экзитах без проверки сертификата - при любом исходе verify.
  [[ "$stage" != 99-verify ]] || warn_insecure_exits
  # HOST_TAG тут остаётся от последнего source-нутого env, подпись была чужой ноды
  [[ $rc -eq 0 ]] || HOST_TAG=deploy die "стадия $stage: одна или несколько нод завершились с ошибкой"

  local elapsed=$(( $(date +%s) - t0 ))
  local mins=$(( elapsed / 60 )) secs=$(( elapsed % 60 ))
  local t="${mins}m ${secs}s"; [[ "$mins" -eq 0 ]] && t="${secs}s"
  _STAGE_LOG+=("$stage"$'\t'"$group"$'\t'"$t")
  if [[ "$stage" == 99-verify && -n "${_VERIFY_WARNINGS[*]:-}" ]]; then
    HOST_TAG=deploy warn '99-verify: ⚠ не все проверки измерены; установка не требует отката'
  fi
  if [[ $incomplete -eq 1 ]]; then return 2; fi
  return 0
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

# Одна нода для preflight_ssh, в фоне: строки для человека - в stdout,
# итог - файлом <host>.ok или <host>.fail с причиной в каталоге $4.
_preflight_host() {
  local h="$1" attempts="$2" budget="$3" dir="$4"
  load_env "$h"
  local ok=0 try reason='' kind=net t0=$SECONDS prc pause auth_tries=0 limit
  local t1 spent n
  if [[ -z "${SSH_PASS:-}" ]]; then
    reason='SSH_PASS пустой — перезапусти ./setup.sh'
  else
    for ((try=1; try<=attempts; try++)); do
      t1=$SECONDS; prc=0; reason=$(ssh_probe) || prc=$?
      if [[ $prc -eq 0 ]]; then ok=1; break; fi
      # Предел в строке - тот, которым цикл закончится при таком же ответе:
      # отказ во входе пробуется дважды, host-key - ни разу больше.
      limit=$attempts
      case $prc in
        2) kind=hostkey; limit=$try ;;
        3) kind=auth; auth_tries=$((auth_tries + 1))
           (( try + 2 - auth_tries >= attempts )) || limit=$((try + 2 - auth_tries)) ;;
        *) kind=net ;;
      esac
      # И тот, что уместится в PREFLIGHT_BUDGET, если следующие попытки
      # ответят так же долго: висящая нода съедает таймаут ssh_probe на каждой,
      # и до пятой не доходит. Расчёт повторяет проверку бюджета ниже.
      spent=$((SECONDS - t0)); n=$try
      while (( n < limit )); do
        pause=$((n * 3))
        [[ $kind != auth || $n -ne $try ]] || pause=${PREFLIGHT_AUTH_PAUSE:-30}
        (( spent + pause + ${SSH_PROBE_TIMEOUT:-30} <= budget )) || break
        spent=$((spent + pause + SECONDS - t1)); n=$((n + 1))
      done
      limit=$n
      printf '  %-30s попытка %d/%d: %s\n' "${SSH_USER}@${SSH_HOST} ($h)" "$try" "$limit" "$reason"
      # Сменившийся host-key ждать бесполезно: его убирает человек.
      [[ $kind != hostkey ]] || break
      pause=$((try * 3))
      # Каждый неверный пароль fail2ban ноды засчитывает адресу entry
      # (maxretry 5 за 10 минут в 00-bootstrap). Повтор один и с паузой:
      # после переустановки ОС провайдер выставляет пароль не сразу. Два
      # запуска подряд дают ноде 4 неудачи - ниже порога бана.
      if [[ $kind == auth ]]; then
        (( auth_tries < 2 )) || break
        pause=${PREFLIGHT_AUTH_PAUSE:-30}
        printf '  %-30s повтор через %d с: если ОС только что переустановили, пароль мог ещё не выставиться\n' \
          "${SSH_USER}@${SSH_HOST} ($h)" "$pause"
      fi
      (( try < attempts )) || break
      if (( SECONDS - t0 + pause + ${SSH_PROBE_TIMEOUT:-30} > budget )); then
        printf '  %-30s время pre-flight на ноду вышло (%d с), больше попыток не будет\n' \
          "${SSH_USER}@${SSH_HOST} ($h)" "$budget"
        break
      fi
      sleep "$pause"
    done
  fi
  if [[ $ok -eq 1 ]]; then
    printf '  %-30s %b✓%b\n' "${SSH_USER}@${SSH_HOST} ($h)" "$C_G" "$C_END"
    : >"$dir/$h.ok"
  else
    printf '  %-30s %b✗ нода %s (%s) недоступна по ssh с entry: %s%b\n' \
      "${SSH_USER}@${SSH_HOST} ($h)" "$C_R" "$h" "$SSH_HOST" "$reason" "$C_END"
    echo "нода $h ($SSH_HOST) недоступна по ssh с entry: $reason" >"$dir/$h.fail"
    echo "$kind" >"$dir/$h.kind"
    [[ $kind != hostkey ]] || ssh_hostkey_fix >"$dir/$h.fix"
  fi
}

preflight_ssh() {
  printf '%b==>%b pre-flight: доступность SSH\n' "$C_B" "$C_END"
  local attempts="${PREFLIGHT_ATTEMPTS:-5}"
  # Потолок на ноду: быстрые отказы (refused, флап sshd) успевают пройти все
  # попытки, а висящая нода отпускается после двух таймаутов ssh_probe.
  local budget="${PREFLIGHT_BUDGET:-90}"

  # ── Per-node SSH check, с ретраями (терпим transient-сбои) ────────────────
  # Ноды опрашиваем разом, вывод печатаем в прежнем порядке: по очереди
  # четыре живые ноды стоили ~3 с, висящая - по 90 с на каждую.
  local hosts; hosts=$(expand_hosts all)
  local reasons=() fixes=() skipped=() pids=() auth=0 net=0 h dir pid; dir=$(mktemp -d)
  for h in $hosts; do
    _preflight_host "$h" "$attempts" "$budget" "$dir" >"$dir/$h.log" 2>&1 &
    pids+=("$!")
  done
  # The deploy lock is another long-lived child; it must outlive preflight.
  for pid in "${pids[@]}"; do wait "$pid" || true; done
  for h in $hosts; do
    cat "$dir/$h.log"
    [[ -f "$dir/$h.ok" ]] && continue
    local reason kind
    reason=$(cat "$dir/$h.fail" 2>/dev/null || echo "нода $h: проверка не выполнилась")
    kind=$(cat "$dir/$h.kind" 2>/dev/null || true)
    # Пропускается только экзит, до которого нет сети. Сменённый host-key и
    # отказ во входе сами не пройдут: это ошибка настройки, деплой стоит.
    if [[ "$h" != ru && $kind != hostkey && $kind != auth ]]; then
      skipped+=("$h")
      printf '  %b⚠ Экзит %s пропущен: SSH недоступен в pre-flight%b\n' "$C_Y" "$h" "$C_END"
      continue
    fi
    reasons+=("$reason")
    case $kind in
      hostkey) fixes+=("$(cat "$dir/$h.fix")") ;;
      auth)    auth=1 ;;
      *)       net=1 ;;
    esac
  done
  rm -rf "$dir"

  SKIPPED_EXIT_TAGS="${skipped[*]}"

  # Недоступный экзит не меняет конфигурацию хаба: его пропускают только
  # fan-out стадии этого запуска. Недоступный entry остаётся стоп-сигналом.
  if [[ ${#reasons[@]} -eq 0 ]]; then
    [[ ${#skipped[@]} -eq 0 ]] || printf '  %b⚠ Продолжаю без экзитов: %s%b\n' "$C_Y" "${skipped[*]}" "$C_END"
    printf '\n'
    return 0
  fi

  printf '\n%b✗ Недоступны по SSH:%b\n' "$C_R" "$C_END"
  printf '  %s\n' "${reasons[@]}"
  if [[ ${#fixes[@]} -gt 0 ]]; then
    printf '  Сменился host-key ноды: так бывает после переустановки ОС на том же IP.\n'
    printf '  Новый ключ сам не принимается - это защита от подмены ноды.\n'
    printf '  Если ОС переустанавливал ты, убери старый ключ и повтори ./deploy.sh:\n'
    printf '    %s\n' "${fixes[@]}"
  fi
  if [[ $auth -eq 1 ]]; then
    printf '  Нода отказала во входе:\n'
    printf '    1. ключ anysda-orchestrator в /root/.ssh/authorized_keys на ноде: после 00-bootstrap экзит пускает только по нему\n'
    printf '    2. пароль root в config.yaml\n'
    printf '    3. PasswordAuthentication=yes в /etc/ssh/sshd_config на ноде\n'
    printf '    4. если ОС на ноде только что переустановили, пароль мог ещё не выставиться: повтори ./deploy.sh через минуту\n'
  fi
  [[ $net -eq 1 ]] && _preflight_net_hint
  HOST_TAG=deploy die "pre-flight: недоступных по ssh нод: ${#reasons[@]}, деплой не запускался"
}

# Подсказка для нод, до чьего sshd не дошли: режет ли провайдер entry
# исходящий :22 или дело в самих нодах.
_preflight_net_hint() {
  # ── Часть нод не пускает — опрашиваем эталонный :22 ТОЛЬКО для диагноза ────
  # Эталон github/gitlab — диагностический фактор, НЕ блокер. Сюда мы
  # попадаем только когда ноды уже не пустили; эталон лишь помогает понять,
  # режет ли провайдер центральной ноды исходящий :22, или дело в самих
  # нодах. Если github/gitlab заблокированы РКН, но ноды живые — этот код
  # вообще не выполняется.
  local ref_ok=0 ref i
  for ((i=1; i<=3; i++)); do
    for ref in github.com gitlab.com; do
      if _tcp22_open "$ref/22"; then ref_ok=1; break 2; fi
    done
    sleep 2
  done

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
  printf '    1. IP ноды в config.yaml\n'
  printf '    2. sshd на ноде запущен и слушает :22\n'
  printf '    3. firewall / geo-блок на стороне провайдера ноды\n'
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
  d=$(printf '%s' "$resp" | grep -oE '"delay":[0-9]+' | sed -n 1p | grep -oE '[0-9]+' || true)
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
  upper=$(echo "$tag" | tr 'a-z-' 'A-Z_')
  # shellcheck disable=SC1091
  ( source "$DEPLOY_ROOT/envs/all.env"; eval "echo \${${upper}_HY2_DIRECT_PORT:-${HY2_DIRECT_PORT:-443}}" )
}

# Перекатывает порт экзита на нодах: 10-foreign на экзите, свежие секреты и
# сертификаты с экзитов (prep_ru_router), 20-ru-router на entry. Без prep
# entry собирался со старыми секретами — ротация не проходила никогда, а
# `>/dev/null || true` это прятал (VPN2-40). Вывод — в лог, на ошибке хвост.
_redeploy_exit_port() {
  local tag="$1" log="$2"
  if ( run_stage_on_host 10-foreign "$tag" && prep_ru_router && run_stage_on_host 20-ru-router ru ) >"$log" 2>&1; then
    return 0
  fi
  printf '%b    ✗ перекатка %s упала, полный вывод: %s%b\n' "$C_R" "$tag" "$log" "$C_END"
  tail -n 5 "$log" | sed 's/^/      /'
  return 1
}

verify_and_rotate_ports() {
  printf '%b==>%b verify: проверяю tunnels via clash-api + UDP-пробник для мертвецов\n' "$C_B" "$C_END"
  local t0; t0=$(date +%s)
  local exits; exits=$(active_exit_tags)
  local rotated_any=0
  local fully_dead=()     # tags, у которых ВЕСЬ UDP-путь RU↔exit фильтруется
  local state_dir="$DEPLOY_ROOT/state"
  mkdir -p "$state_dir"

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
      python3 "$DEPLOY_ROOT/lib/config-set-port.py" "$DEPLOY_ROOT/.." "$tag" "$port" >/dev/null
      python3 "$DEPLOY_ROOT/lib/config2env.py"     "$DEPLOY_ROOT/.." >/dev/null
      _redeploy_exit_port "$tag" "$state_dir/rotate-${tag}-${port}.log" || continue
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
      python3 "$DEPLOY_ROOT/lib/config-set-port.py" "$DEPLOY_ROOT/.." "$tag" "${HY2_DIRECT_PORT:-443}" >/dev/null
      python3 "$DEPLOY_ROOT/lib/config2env.py"     "$DEPLOY_ROOT/.." >/dev/null
      # Откат только в config.yaml оставлял экзит и entry на последнем
      # кандидате — до следующего деплоя ноды и конфиг расходились.
      _redeploy_exit_port "$tag" "$state_dir/rotate-${tag}-rollback.log" || true
    fi
  done

  # Persist excluded exits + регенерим envs + redeploy 20-ru-router чтобы
  # sing-box, vmagent (25-monitoring) и панель (30-frontend) увидели
  # отфильтрованный EXIT_TAGS на последующих стадиях.
  local state_file="$state_dir/excluded-exits.txt"
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
    # 20-ru-router идёт только на entry и секреты экзитов берёт из файла, с
    # мёртвыми нодами не связывается — недоступный экзит уронить её не может.
    # Упала — значит сломан entry, и sing-box мог остаться на старом конфиге.
    local reroute_log="$state_dir/excluded-reroute.log"
    if ! run_stage_on_host 20-ru-router ru >"$reroute_log" 2>&1; then
      printf '%b    ✗ 20-ru-router без исключённых экзитов упал, полный вывод: %s%b\n' "$C_R" "$reroute_log" "$C_END"
      tail -n 15 "$reroute_log" | sed 's/^/      /'
      die "verify_and_rotate_ports: entry не пересобран после исключения экзитов"
    fi
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
    _STAGE_LOG+=($'[port-rotate]\tforeign\t'"$t")
  fi
  if [[ ${#fully_dead[@]} -gt 0 ]]; then
    _STAGE_LOG+=($'[excluded]\t'"${fully_dead[*]}"$'\t'"$t")
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
# Образы панели и бота тянем в фоне, пока ru занята apt-стадиями 26/27:
# 30/35 потом делают свой docker pull по уже скачанным слоям. Сбой
# предзагрузки не страшен - стадии тянут образ сами и сами падают без него.
# Имена образов - те же умолчания, что в 30-frontend.sh / 35-telegram.sh.
prefetch_images() {
  (
    load_env ru
    if [[ "${VPN2_DEVELOPMENT:-false}" != true ]]; then
      ssh_exec 'python3 /tmp/anysda/release.py --manifest /tmp/anysda/release-manifest.json image panel --offline' >/dev/null
      exit
    fi
    local imgs=()
    [[ "${PANEL_IMAGE_PULL:-true}" == false ]] || imgs+=("${PANEL_IMAGE:-ghcr.io/anysda/vpn2/panel:dev}")
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" || "${TGBOT_IMAGE_PULL:-true}" == false ]] \
      || imgs+=("${TGBOT_IMAGE:-ghcr.io/anysda/vpn2/tgbot:dev}")
    [[ ${#imgs[@]} -gt 0 ]] || exit 0
    if ssh_exec "for i in ${imgs[*]}; do docker pull -q \"\$i\" || exit 1; done" >/dev/null 2>&1; then
      log "предзагрузка образов: ${imgs[*]}"
    else
      warn "предзагрузка образов не вышла - стадии потянут сами"
    fi
  ) &
  _PREFETCH_PID=$!
}

# Несколько цепочек стадий параллельно; внутри цепочки стадии идут по порядку.
# Цепочка - строка "стадия:группа стадия:группа". Ждём все цепочки, потом
# падаем, если упала хоть одна: брошенная на полпути цепочка хуже ожидания.
run_tracks() {
  local track pids=() outs=() rc=0 i
  for track in "$@"; do
    local out; out=$(mktemp)
    outs+=("$out")
    (
      _STAGE_LOG=()
      for s in $track; do run_stage "${s%%:*}" "${s#*:}"; done
      printf '%s\n' "${_STAGE_LOG[@]}" > "$out"
    ) &
    pids+=("$!")
  done
  for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || rc=1
    [[ -s "${outs[$i]}" ]] && mapfile -t -O "${#_STAGE_LOG[@]}" _STAGE_LOG < "${outs[$i]}"
    rm -f "${outs[$i]}"
  done
  [[ $rc -eq 0 ]] || HOST_TAG=deploy die "параллельные стадии ($*): одна из цепочек завершилась с ошибкой"
}

do_all() {
  print_banner
  _TOTAL_T0=$(date +%s)
  _STAGE_LOG=()
  _VERIFY_WARNINGS=()

  pause_entry_auto_updates
  install_prereqs
  do_check
  preflight_ssh

  run_stage 00-bootstrap     ru
  run_stage 05-mgmt-mesh     ru
  # Even bootstrap/firewall and management changes belong to the exit's
  # sequential update. Stage 10 verifies it before the next exit starts.
  local exit_track='' tag
  for tag in $(expand_hosts foreign); do
    exit_track+="00-bootstrap:$tag 05-mgmt-mesh:$tag 10-foreign:$tag "
  done
  # 28/29/19 на ru от экзитов не зависят (секреты экзитов забирает только
  # prep_ru_router перед 20) - гоняем их, пока экзиты ставят 10-foreign.
  # 19 строго ДО 20: сначала готовим путь десинка YouTube, потом sing-box
  # начинает на него маршрутизировать. Обратный порядок = окно, в котором
  # YouTube уже на РФ-выходе, а обходить DPI ещё нечем. Стадия сама валит
  # деплой, если A/B-проверка не прошла.
  run_tracks "$exit_track" \
             "28-wireguard:ru 29-openvpn:ru 19-yt-zapret:ru"
  run_stage 20-ru-router     ru
  verify_and_rotate_ports
  run_stage 21-failover-watchdog ru
  run_stage 22-adguard       ru
  run_stage 23-emergency     all
  run_stage 25-monitoring    ru
  prefetch_images
  run_stage 26-backup        ru
  run_stage 27-ikev2         ru
  run_stage 29-control       ru
  wait "$_PREFETCH_PID" || true
  VPN2_PANEL_FOLLOWS=true run_stage 35-telegram ru
  run_stage 30-frontend      ru
  local verify_rc=0
  run_stage 99-verify        all || verify_rc=$?
  [[ $verify_rc -ne 1 ]] || return 1

  release_complete
  print_summary
  announce_deploy_done
  # 2 — установка прошла, но не всё измерено: код доходит до CI.
  return "$verify_rc"
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
  local unmeasured=false
  [[ -z "${_VERIFY_WARNINGS[*]:-}" ]] || unmeasured=true
  ssh_exec "[ -f /etc/anysda/tgbot-secret.txt ] && curl -fsS --max-time 5 \
      -H 'Content-Type: application/json' \
      -H \"X-Tgbot-Secret: \$(cat /etc/anysda/tgbot-secret.txt)\" \
      -d '{\"type\":\"deploy_done\",\"verify_unmeasured\":$unmeasured}' \
      http://127.0.0.1:8877/event" >/dev/null 2>&1 || true
}

# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
usage() {
  cat <<EOF
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
EOF
  exit 0
}

# ----------------------------------------------------------------------------
# backup/restore/backup-list — выполняются на entry через ssh_exec.
# ----------------------------------------------------------------------------
do_backup() {
  load_env ru
  local rc=0
  ssh_exec '/usr/local/bin/anysda-backup.sh' || rc=$?
  # Код 4: архив снят, но БД и конфиги VPN расходятся (устройства в выводе
  # выше). Архив годный - команда удалась, предупреждение и в «Событиях».
  if [[ $rc -eq 4 ]]; then
    warn 'архив сохранён с предупреждением: БД и конфиги VPN расходятся - перевыпусти перечисленные выше устройства в панели'
    return 0
  fi
  return "$rc"
}

do_backup_list() {
  load_env ru
  ssh_exec '/usr/local/bin/anysda-backup-list.sh'
}

do_restore() {
  # Имя архива необязательно: `restore --dry-run` - это latest с флагом.
  local archive=latest
  if [[ -n "${1:-}" && "$1" != -* ]]; then archive=$1; shift; fi
  local flags="" flag dry_run=0
  for flag in "$@"; do
    [[ -n "$flag" ]] || continue
    flags+=" '$flag'"
    [[ "$flag" != --dry-run ]] || dry_run=1
  done
  local RESTORE_PRESERVE_ADMIN=1
  load_env ru
  # Если архив — локальный путь на оркестраторе, scp'им на entry.
  # Иначе (имя файла или 'latest') — entry резолвит сам.
  if [[ "$archive" != "latest" && -f "$archive" ]]; then
    local base; base=$(basename "$archive")
    if (( dry_run )); then
      echo "restore: dry-run — загрузил бы на entry и восстановил локальный архив $base, ничего не изменено"
      return 0
    fi
    log "scp локального архива на entry: $base"
    ssh_exec "mkdir -p \${BACKUP_LOCAL_DIR:-/var/backups/anysda-vpn2} 2>/dev/null || mkdir -p /var/backups/anysda-vpn2"
    push "$archive" "$base"
    ssh_exec "mv /tmp/anysda/'$base' '/var/backups/anysda-vpn2/$base' && chmod 600 '/var/backups/anysda-vpn2/$base'"
    archive="$base"
  fi
  ssh_exec "/usr/local/bin/anysda-restore.sh '$archive'$flags"
  (( ! dry_run )) || return 0
  # Архив возвращает /etc/anysda/* и конфиги OpenVPN/IKEv2 того деплоя, с
  # которого он снят, а запущенные службы собраны под секреты текущего: бот
  # отвечает панели 403, вотчдог получает 401 от clash-api, у OpenVPN из
  # старого server.conf пропадает management, сертификат IKEv2 выписан на
  # прежний IP. Пересобираем всё, что читает восстановленное (ключи и PKI
  # стадии не трогают).
  local s
  for s in 29-openvpn 20-ru-router 21-failover-watchdog 26-backup 27-ikev2 35-telegram 30-frontend; do
    run_stage "$s" ru
  done
}

# Читает список тегов выходных нод из envs/exits.env
exit_tags() {
  local exits_file="$DEPLOY_ROOT/envs/exits.env"
  [[ -f "$exits_file" ]] || die "envs/exits.env не найден — запусти ./setup.sh"
  # shellcheck disable=SC1090
  source "$exits_file"
  echo "${EXIT_TAGS:-}"
}

# Без аргументов → полный pipeline.
# С двумя аргументами → одна стадия на группу.
# -h / --help → подсказка.
case "${1:-}" in -h|--help) usage ;; esac
# shellcheck source=infra/lib/deploy-release.sh
source "$DEPLOY_ROOT/lib/deploy-release.sh"
# Ошибка в аргументах стадии не открывает выпуск: иначе запись остаётся в
# applying, и следующий деплой требует recover.
case "${1:-}" in
  ''|recover|backup|backup-list|restore|rollback) ;;
  *)
    [[ $# -ge 2 ]] || die "использование: ./deploy.sh <stage> <group>  (пример: ./deploy.sh 30-frontend ru)"
    [[ -f "$DEPLOY_ROOT/scripts/$1.sh" ]] || die "нет такой стадии: $1 (список: ./deploy.sh --help)"
    expand_hosts "$2" >/dev/null
    ;;
esac
release_start "$@"
case "${1:-}" in
  ''|recover)  do_all ;;
  -h|--help)   usage ;;
  backup)      do_backup ;;
  backup-list) do_backup_list ;;
  restore)     do_restore "${@:2}" ;;
  rollback)    release_rollback ;;
  *)
    run_stage "$1" "$2"
    if [[ "$1" == 30-frontend && ( "$2" == ru || "$2" == all ) ]]; then
      print_sso_notice
    fi
    ;;
esac
release_complete
