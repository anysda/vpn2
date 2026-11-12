# shellcheck shell=bash
# Общие приёмы стадий деплоя и anysda-restore: перезапуск служб только при смене
# их входов, apt без молчаливого ожидания блокировок.

SVC_FP_DIR=${SVC_FP_DIR:-/var/lib/anysda/applied}

# release_download <имя> <файл> <url> [sha256]: vpn2.release с повтором и сверкой хеша;
# хеш из манифеста сборки, в режиме разработки - четвёртым аргументом.
release_download() {
  local name="$1" destination="$2" url="$3" sha="${4:-}" src manifest=()
  src=$(dirname "${BASH_SOURCE[0]}")
  [[ -d "$src/vpn2/release" ]] || src=$src/../vpn2/src
  if [[ "${VPN2_DEVELOPMENT:-false}" != true ]]; then
    manifest=(--manifest "${VPN2_RELEASE_MANIFEST:-/tmp/anysda/release-manifest.json}")
  fi
  PYTHONPATH=$src python3 -m vpn2.release "${manifest[@]}" artifact "$name" "$destination" --url "$url" ${sha:+--sha256 "$sha"}
}

svc_say() { echo "[${HOST_TAG:-anysda}]   $*"; }

# nftables.service не сбрасывает весь ruleset: в нём таблицы ufw, Docker, fail2ban.
nftables_preserve_rules() {
  local conf=/etc/nftables.conf
  if [[ -f "$conf" ]] && grep -Eq '^[[:space:]]*flush[[:space:]]+ruleset[[:space:]]*;?[[:space:]]*(#.*)?$' "$conf"; then
    [[ -e "$conf.before-vpn2" ]] || cp -a "$conf" "$conf.before-vpn2"
    sed -i -E 's/^[[:space:]]*flush[[:space:]]+ruleset[[:space:]]*;?[[:space:]]*(#.*)?$/# vpn2: preserve tables owned by ufw, routing, Docker and fail2ban/' "$conf"
  fi
  mkdir -p /etc/systemd/system/nftables.service.d
  cat > /etc/systemd/system/nftables.service.d/10-anysda-preserve.conf <<'EOF'
[Service]
# Stopping this loader must not remove rules owned by other services.
ExecStop=
EOF
  systemctl daemon-reload
}

# Был ли перезапуск через svc_restart_if_changed с прошлого svc_settle.
SVC_RESTARTED=0

# svc_restart_if_changed UNIT FILE...: рестарт, если служба не идёт или отпечаток файлов,
# что она читает при старте, не тот, что при прошлом рестарте.
svc_restart_if_changed() {
  local unit=$1 fp_file fp
  shift
  fp_file="$SVC_FP_DIR/$unit.sha256"
  fp=$({ cat "$@" 2>/dev/null || true; } | sha256sum | cut -d' ' -f1)
  if systemctl is-active --quiet "$unit" && [[ "$(cat "$fp_file" 2>/dev/null)" == "$fp" ]]; then
    svc_say "$unit: конфиг, сертификаты и бинарник прежние — не перезапускаю"
    return 0
  fi
  systemctl restart "$unit" || return
  SVC_RESTARTED=1
  mkdir -p "$SVC_FP_DIR"
  echo "$fp" > "$fp_file"
  svc_say "$unit перезапущен"
}

# svc_settle SEC [ПРОБА...]: после рестарта ждёт пробу до SEC с, без пробы - SEC с.
svc_settle() {
  local sec=$1 i
  shift
  if (( SVC_RESTARTED )); then
    if (( $# )); then
      for (( i = 0; i < sec * 5; i++ )); do
        "$@" && break
        sleep 0.2
      done
    else
      sleep "$sec"
    fi
  fi
  SVC_RESTARTED=0
}

# wg0_apply: wg syncconf без обрыва, рестарт wg-quick - только без интерфейса или
# при других адресах. Address читается как у wg-quick.
wg0_conf_iface() {
  awk '
    { sub(/#.*/, ""); key = $0; sub(/=.*/, "", key); value = $0; sub(/^[^=]*=/, "", value)
      gsub(/^[ \t\r\v\f]+|[ \t\r\v\f]+$/, "", key); gsub(/^[ \t\r\v\f]+|[ \t\r\v\f]+$/, "", value) }
    key ~ /^\[/ { interface = tolower(key) == "[interface]"; next }
    interface && tolower(key) == "address" && /=/ {
      n = split(value, a, /[, \t]+/); for (i = 1; i <= n; i++) if (a[i] != "") print "addr " a[i]
    }' /etc/wireguard/wg0.conf | sort
}
wg0_apply() {
  if systemctl is-active --quiet wg-quick@wg0 && ip link show wg0 >/dev/null 2>&1 \
     && [[ "$(wg0_conf_iface)" == "$(ip -o addr show dev wg0 scope global | awk '{print "addr " $4}' | sort)" ]]; then
    wg syncconf wg0 <(wg-quick strip wg0) || return
    svc_say "адреса wg0 прежние — wg syncconf без рестарта"
  else
    systemctl restart wg-quick@wg0 || return
    svc_say "wg-quick@wg0 перезапущен"
  fi
}

# apt и dpkg: перед apt ждём блокировки сами и раз в 20 с пишем, кого ждём, в stdout
# стадии на момент подключения библиотеки (`apt_get ... >/dev/null` строку не глушит).
exec {APT_LOG_FD}>&1
APT_LOCKS=(/var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock)
APT_LOCK_TIMEOUT=1200
# Метка исчерпанного ожидания живёт час: следующий вызов не ждёт, Lock::Timeout короткий.
APT_GAVE_UP=${APT_GAVE_UP:-/run/anysda-apt-gave-up}
APT_GAVE_UP_LOCK_TIMEOUT=60

apt_gave_up() {
  [[ -n "$(find "$APT_GAVE_UP" -mmin -60 2>/dev/null)" ]]
}

# fuser, а без psmisc - lslocks из util-linux.
apt_lock_holders() {
  if command -v fuser >/dev/null 2>&1; then
    fuser "${APT_LOCKS[@]}" 2>/dev/null
  else
    lslocks --noheadings --notruncate --output PID,PATH | awk -v locks=" ${APT_LOCKS[*]} " '
      NF == 2 && index(locks, " " $2 " ") { print $1; found = 1 }
      END { exit !found }'
  fi
}

apt_wait_idle() {
  local t0=$SECONDS said=-20 holders pid
  while holders=$(apt_lock_holders); do
    if apt_gave_up; then
      svc_say "apt всё ещё занят, ожидание уже исчерпано, продолжаю" >&"$APT_LOG_FD"
      return 0
    fi
    if (( SECONDS - t0 >= APT_LOCK_TIMEOUT )); then
      svc_say "apt так и не освободился за $(( SECONDS - t0 )) с, продолжаю" >&"$APT_LOG_FD"
      : >"$APT_GAVE_UP" 2>/dev/null || true
      return 0
    fi
    if (( SECONDS - said >= 20 )); then
      said=$SECONDS
      for pid in $(tr -s ' ' '\n' <<<"$holders" | sort -un); do
        svc_say "apt занят, жду $pid $(tr '\0' ' ' </proc/"$pid"/cmdline 2>/dev/null)$(( SECONDS - t0 )) с" >&"$APT_LOG_FD"
      done
    fi
    sleep 5
  done
  rm -f "$APT_GAVE_UP" 2>/dev/null || true
  (( SECONDS - t0 < 5 )) || svc_say "apt освободился через $(( SECONDS - t0 )) с" >&"$APT_LOG_FD"
}

_unit_running() { [[ "$(systemctl show -P ActiveState "$1" 2>/dev/null)" == @(activating|active|deactivating) ]]; }

# apt_auto_pause: автообновления на паузу маской --runtime; через час её снимает таймер.
apt_auto_pause() {
  local timers=(apt-daily.timer apt-daily-upgrade.timer)
  local jobs=(apt-daily.service apt-daily-upgrade.service)
  local units=("${timers[@]}" "${jobs[@]}" unattended-upgrades.service)
  local t0=$SECONDS said=-20 j busy
  # Таймеры: стоп до маски, иначе активный уходит в failed.
  systemctl stop "${timers[@]}" >/dev/null 2>&1 || true
  systemctl mask --runtime "${timers[@]}" >/dev/null 2>&1 || true
  systemctl reset-failed "${timers[@]}" >/dev/null 2>&1 || true
  # Служба в ExecStartPre ещё ничего не начала - снимаем; идущую дожидаемся.
  for j in "${jobs[@]}"; do
    if [[ "$(systemctl show -P SubState "$j" 2>/dev/null)" == start-pre ]]; then
      systemctl stop "$j" >/dev/null 2>&1 || true
    fi
  done
  while busy=$(for j in "${jobs[@]}"; do ! _unit_running "$j" || printf '%s ' "$j"; done) && [[ -n "$busy" ]]; do
    if (( SECONDS - t0 >= APT_LOCK_TIMEOUT )); then
      svc_say "автообновление так и не закончилось за $(( SECONDS - t0 )) с, продолжаю" >&"$APT_LOG_FD"
      break
    fi
    if (( SECONDS - said >= 20 )); then
      said=$SECONDS
      svc_say "идёт автообновление, жду ${busy}$(( SECONDS - t0 )) с" >&"$APT_LOG_FD"
    fi
    sleep 2
  done
  (( SECONDS - t0 < 5 )) || svc_say "автообновление закончилось через $(( SECONDS - t0 )) с" >&"$APT_LOG_FD"
  # Маска идущей oneshot-службы сократила бы её TimeoutStartSec до 90 с.
  for j in "${jobs[@]}"; do
    _unit_running "$j" || systemctl mask --runtime "$j" >/dev/null 2>&1 || true
  done
  apt_wait_idle
  systemctl mask --runtime --now unattended-upgrades.service >/dev/null 2>&1 || true
  systemctl stop anysda-apt-resume.timer >/dev/null 2>&1 || true
  systemd-run --quiet --unit=anysda-apt-resume --on-active=1h \
    /bin/sh -c "systemctl unmask --runtime ${units[*]}
      for u in ${timers[*]} unattended-upgrades.service; do if systemctl is-enabled --quiet \$u; then systemctl start \$u; fi; done" \
    || svc_say "таймер возврата автообновлений не поставлен, вернутся после перезагрузки"
}

# apt_get ARGS...: apt-get после apt_wait_idle. Сбой загрузки (5xx, 408, 429, обрыв)
# повторяется до APT_FETCH_TRIES раз, прочие 4xx - нет; итог - строка «зеркало apt не отдало».
APT_FETCH_TRIES=3
apt_get() {
  local try=1 rc err failed lock_timeout
  err=$(mktemp)
  while :; do
    apt_wait_idle
    lock_timeout=$APT_LOCK_TIMEOUT
    ! apt_gave_up || lock_timeout=$APT_GAVE_UP_LOCK_TIMEOUT
    rc=0
    apt-get -o DPkg::Lock::Timeout="$lock_timeout" "$@" 2>"$err" || rc=$?
    failed=$(awk '/^[WE]: Failed to fetch / {
        if ($6 ~ /^4[0-9][0-9]$/ && $6 != 408 && $6 != 429) next
        url = $5; sub(/^[WE]: Failed to fetch +[^ ]+ +/, ""); sub(/ +\[IP: .*/, "")
        gsub(/  +/, " "); print url ": " $0; exit }' "$err")
    [[ -n "$failed" ]] || break
    if (( try >= APT_FETCH_TRIES )); then
      cat "$err" >&2
      svc_say "зеркало apt не отдало ${failed} за ${try} попытки" >&2
      rm -f "$err"
      return "$rc"
    fi
    svc_say "зеркало apt: ${failed}, повтор через $((5 * try)) с" >&"$APT_LOG_FD"
    sleep $((5 * try))
    try=$((try + 1))
  done
  cat "$err" >&2
  rm -f "$err"
  return "$rc"
}

# deb_install FILE: без зависимостей - dpkg -i, с зависимостями - apt.
deb_install() {
  if [[ -n "$(dpkg-deb -f "$1" Depends Pre-Depends 2>/dev/null)" ]]; then
    apt_get install -y -qq "$1"
    return
  fi
  apt_wait_idle
  DEBIAN_FRONTEND=noninteractive dpkg --force-confdef --force-confold -i "$1"
}

# Caddy - .deb из GitHub-релиза, ставится поверх любой другой версии.
CADDY_VER='2.11.9'
# GitHub release assets API, tag v2.11.9, поле digest.
CADDY_SHA256='bbea4aeaadc86b233c168cc33bad9daa1cbce756dc08949b783b45398f8e65cb'
caddy_install() (
  local deb="caddy_${CADDY_VER}_linux_amd64.deb" tmp
  [[ "$(caddy version 2>/dev/null | cut -d' ' -f1)" != "v${CADDY_VER}" ]] || exit 0
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  release_download caddy "$tmp/$deb" "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VER}/$deb" "$CADDY_SHA256" \
    && deb_install "$tmp/$deb" >/dev/null
)

# apt_have PKG...: все пакеты уже установлены.
apt_have() {
  local st
  st=$(dpkg-query -W -f='${db:Status-Status}\n' "$@" 2>/dev/null) || return 1
  ! grep -qvx installed <<<"$st"
}

# docker_apt_source: APT-репо Docker; файл источника переписывается только при отличии.
# DOCKER_PKGS и DOCKER_SKIP читают роли emergency и containers.
# shellcheck disable=SC2034
DOCKER_PKGS=(docker-ce docker-ce-cli containerd.io)
# shellcheck disable=SC2034
DOCKER_SKIP=(docker-ce-rootless-extras- docker-buildx-plugin- docker-compose-plugin-)
docker_apt_source() {
  local list=/etc/apt/sources.list.d/docker.list line distro codename release
  release=$(. /etc/os-release && printf '%s %s' "${ID:-}" "${VERSION_CODENAME:-}") || return
  read -r distro codename <<<"$release"
  case "$distro" in
    debian|ubuntu) ;;
    *) svc_say "Docker: неподдерживаемый ID в /etc/os-release: $distro" >&2; return 1 ;;
  esac
  if [[ ! "$codename" =~ ^[a-z][a-z0-9-]*$ ]]; then
    svc_say "Docker: нет корректного VERSION_CODENAME в /etc/os-release" >&2
    return 1
  fi
  if ! apt_have ca-certificates curl gnupg; then
    apt_get update -qq
    apt_get install -y -qq ca-certificates curl gnupg >/dev/null
  fi
  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -s /etc/apt/keyrings/docker.gpg ]]; then
    curl -fsSL "https://download.docker.com/linux/$distro/gpg" \
      | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
  fi
  line="deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$distro $codename stable"
  [[ "$(cat "$list" 2>/dev/null)" == "$line" ]] || echo "$line" > "$list"
}

# apt_update_if_stale: apt-get update, если списки старше часа, источники новее их или
# в каталоге списков нет *_Packages*.
apt_update_if_stale() {
  local lists=/var/lib/apt/lists
  [[ -n "$(find "$lists" -maxdepth 0 -mmin -60 2>/dev/null)" ]] \
    && [[ -z "$(find /etc/apt/sources.list /etc/apt/sources.list.d -newer "$lists" -print -quit 2>/dev/null)" ]] \
    && [[ -n "$(find "$lists" -maxdepth 1 -type f -name '*_Packages*' -print -quit 2>/dev/null)" ]] \
    || apt_get update -qq
}

# apt_prepare: Lock::Timeout для любого apt на узле, ожидание блокировок, довод
# прерванного dpkg.
apt_prepare() {
  printf 'DPkg::Lock::Timeout "%s";\n' "$APT_LOCK_TIMEOUT" > /etc/apt/apt.conf.d/90anysda-lock-timeout
  apt_wait_idle
  if [[ -n "$(ls -A /var/lib/dpkg/updates 2>/dev/null)" ]]; then
    svc_say "dpkg был прерван, довожу: dpkg --configure -a"
    DEBIAN_FRONTEND=noninteractive dpkg --configure -a --force-confdef --force-confold
  fi
}

# apt_sshd_first: до первой 00-bootstrap и до сторожа фаервола обновляет openssh
# отдельным заходом и ждёт баннер sshd; не дождался - отказ до правок фаервола.
APT_SSHD_STAMP=/var/anysda/.stamps/00-bootstrap
APT_SSHD_WAIT=60
apt_sshd_versions() {
  LC_ALL=C apt-cache policy openssh-server 2>/dev/null \
    | awk '$1 == "Installed:" { i = $2 } $1 == "Candidate:" { c = $2 } END { print i, c }'
}

# Баннер sshd по адресу и порту, которыми пришёл деплой.
sshd_answers() {
  local addr port
  read -r _ _ addr port <<<"${SSH_CONNECTION:-}"
  # shellcheck disable=SC2016
  timeout 5 bash --norc -c 'exec 3<>"/dev/tcp/$0/$1" && read -r -t 3 banner <&3 && [[ $banner == SSH-* ]]' \
    "${addr:-127.0.0.1}" "${port:-22}" 2>/dev/null
}

apt_sshd_first() {
  local installed candidate t0
  [[ ! -e "$APT_SSHD_STAMP" ]] || return 0
  apt_have openssh-server || return 0
  apt_update_if_stale || true
  read -r installed candidate < <(apt_sshd_versions) || true
  [[ -n "$candidate" && "$candidate" != "(none)" && "$installed" != "$candidate" ]] || return 0
  svc_say "openssh-server $installed -> $candidate: обновляю до включения сторожа фаервола"
  DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt_get install -y -qq --only-upgrade \
    -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
    openssh-server openssh-client openssh-sftp-server >/dev/null
  t0=$SECONDS
  until sshd_answers; do
    if (( SECONDS - t0 >= APT_SSHD_WAIT )); then
      svc_say "sshd не отвечает $((SECONDS - t0)) с после обновления openssh-server, фаервол не тронут: journalctl -u ssh"
      return 1
    fi
    sleep 2
  done
}
