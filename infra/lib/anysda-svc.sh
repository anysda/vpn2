# shellcheck shell=bash
# Общие приёмы стадий деплоя и anysda-restore: не рвать клиентов зря и не
# стоять молча на блокировке apt.
#
# Рестарт службы обрывает сессии: IKEv2-устройства после рестарта strongswan
# сами не переподключаются, у WireGuard обнуляются рукопожатия. Поэтому
# службу перезапускаем, только если она читает уже не то, с чем стартовала,
# тем же отпечатком, что у sing-box в 10-foreign и 20-ru-router.

SVC_FP_DIR=${SVC_FP_DIR:-/var/lib/anysda/applied}

svc_say() { echo "[${HOST_TAG:-anysda}]   $*"; }

# Был ли перезапуск через svc_restart_if_changed с прошлого svc_settle.
SVC_RESTARTED=0

# svc_restart_if_changed UNIT FILE...
# FILE — всё, что служба читает при старте: бинарник, юнит, конфиги,
# сертификаты. Отпечаток их содержимого сверяем с записанным при прошлом
# рестарте; нет файла — нет его и в отпечатке.
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

# svc_settle SEC — дать только что перезапущенной службе подняться (или
# упасть) до показа статуса. Если ничего не перезапускали, ждать нечего.
svc_settle() {
  (( SVC_RESTARTED )) && sleep "$1"
  SVC_RESTARTED=0
}

# svc_forget UNIT — вход службы поменялся так, что отпечаток файлов этого не
# заметит. Следующий svc_restart_if_changed её перезапустит.
svc_forget() { rm -f "$SVC_FP_DIR/$1.sha256"; }

# wg0_apply — применить /etc/wireguard/wg0.conf. Ключи и пиры `wg syncconf`
# меняет на живом интерфейсе без обрыва. Рестарт wg-quick нужен, только если
# интерфейса нет или в wg0.conf другие адреса или порт.
wg0_conf_iface() {
  awk -F= '
    /^\[/ { sec = $0; next }
    sec == "[Interface]" {
      k = $1; v = $2; gsub(/[ \t]/, "", k); gsub(/[ \t]/, "", v)
      if (k == "Address") { n = split(v, a, ","); for (i = 1; i <= n; i++) print "addr " a[i] }
      if (k == "ListenPort") print "port " v
    }' /etc/wireguard/wg0.conf | sort
}
wg0_live_iface() {
  { ip -o addr show dev wg0 scope global | awk '{print "addr " $4}'
    echo "port $(wg show wg0 listen-port)"; } | sort
}
wg0_apply() {
  if systemctl is-active --quiet wg-quick@wg0 && ip link show wg0 >/dev/null 2>&1 \
     && [[ "$(wg0_conf_iface)" == "$(wg0_live_iface)" ]]; then
    wg syncconf wg0 <(wg-quick strip wg0) || return
    svc_say "адреса и порт wg0 прежние — wg syncconf без рестарта"
  else
    systemctl restart wg-quick@wg0 || return
    svc_say "wg-quick@wg0 перезапущен"
  fi
}

# ── apt/dpkg ────────────────────────────────────────────────────────────────
# На свежей Ubuntu apt-daily и unattended-upgrades минутами держат блокировки
# apt/dpkg. Штатный DPkg::Lock::Timeout ждёт их молча, а `apt-get update`
# (блокировку списков) не ждёт вовсе и сразу падает, поэтому перед apt
# ждём сами и раз в 20 с пишем, кого ждём. Пишем в stdout стадии, каким он был
# при подключении библиотеки: `apt_get install ... >/dev/null` не глушит строку.
exec {APT_LOG_FD}>&1
APT_LOCKS=(/var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock)
APT_LOCK_TIMEOUT=1200

apt_wait_idle() {
  local t0=$SECONDS said=-20 holders pid
  while holders=$(fuser "${APT_LOCKS[@]}" 2>/dev/null); do
    if (( SECONDS - t0 >= APT_LOCK_TIMEOUT )); then
      svc_say "apt так и не освободился за $(( SECONDS - t0 )) с, продолжаю" >&"$APT_LOG_FD"
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
  (( SECONDS - t0 < 5 )) || svc_say "apt освободился через $(( SECONDS - t0 )) с" >&"$APT_LOG_FD"
}

# apt_auto_pause — автообновления Ubuntu на паузу до конца деплоя.
# Через минуту после загрузки apt-daily запускает unattended-upgrade, и
# следующие стадии минутами стоят на блокировке apt. Выключаем только на время
# деплоя, не насовсем: своей политики обновлений у узлов нет, патчи
# безопасности ставит только unattended-upgrades. Маска --runtime живёт до
# перезагрузки, а через час её снимает таймер, даже если деплой упал.
apt_auto_pause() {
  local timers=(apt-daily.timer apt-daily-upgrade.timer)
  local jobs=(apt-daily.service apt-daily-upgrade.service)
  local units=("${timers[@]}" "${jobs[@]}" unattended-upgrades.service)
  local t0=$SECONDS said=-20 j busy
  systemctl mask --runtime --now "${timers[@]}" >/dev/null 2>&1 || true
  # Сработавший таймер оставляет службу в ExecStartPre: она до 30 с ждёт
  # сеть и apt ещё не держит, а через минуту схватит его посреди деплоя.
  # Такую снимаем, ничего не начато. Уже идущую дожидаемся, а не рвём
  # посреди dpkg.
  for j in "${jobs[@]}"; do
    if [[ "$(systemctl show -P SubState "$j" 2>/dev/null)" == start-pre ]]; then
      systemctl stop "$j" >/dev/null 2>&1 || true
    fi
  done
  # Обе службы oneshot: пока работают, они в activating, а не active.
  while busy=$(for j in "${jobs[@]}"; do
      [[ "$(systemctl show -P ActiveState "$j" 2>/dev/null)" == @(activating|active|deactivating) ]] && printf '%s ' "$j"
    done) && [[ -n "$busy" ]]; do
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
  # Маску на службы - только когда они стоят: маска перечитывает юнит, и у
  # идущей oneshot-службы TimeoutStartSec падает с infinity до 90 с - systemd
  # убил бы unattended-upgrade посреди установки.
  for j in "${jobs[@]}"; do
    [[ "$(systemctl show -P ActiveState "$j" 2>/dev/null)" == @(activating|active|deactivating) ]] \
      || systemctl mask --runtime "$j" >/dev/null 2>&1 || true
  done
  apt_wait_idle
  systemctl mask --runtime --now unattended-upgrades.service >/dev/null 2>&1 || true
  systemctl stop anysda-apt-resume.timer >/dev/null 2>&1 || true
  # Возврат: снять все маски, запустить снова таймеры и службу
  # unattended-upgrades (она только доводит обновление при выключении).
  systemd-run --quiet --unit=anysda-apt-resume --on-active=1h \
    /bin/sh -c "systemctl unmask --runtime ${units[*]}
      for u in ${timers[*]} unattended-upgrades.service; do if systemctl is-enabled --quiet \$u; then systemctl start \$u; fi; done" \
    || svc_say "таймер возврата автообновлений не поставлен, вернутся после перезагрузки"
}

# apt_get ARGS... — apt-get после apt_wait_idle; гонку между проверкой и
# стартом закрывает штатный DPkg::Lock::Timeout. Он ждёт только dpkg, а
# блокировки списков и архива apt не ждёт (rc=100 сразу), поэтому стадии,
# идущие на узле параллельно, ставят пакеты по очереди через общий flock.
APT_FLOCK=/run/anysda-apt.lock
apt_get() {
  apt_wait_idle
  flock -w "$APT_LOCK_TIMEOUT" "$APT_FLOCK" \
    apt-get -o DPkg::Lock::Timeout="$APT_LOCK_TIMEOUT" "$@"
}

# apt_update_if_stale — apt-get update, только если списки старше часа или
# источники правили после них (свежее репо docker). Правило то же, что в
# 26/27/29: на чистой установке entry обновляет списки в install_prereqs,
# и bootstrap через полминуты повторял бы то же самое (~4 с).
apt_update_if_stale() {
  local lists=/var/lib/apt/lists
  if [[ -n "$(find "$lists" -maxdepth 0 -mmin -60 2>/dev/null)" ]] &&
     [[ -z "$(find /etc/apt/sources.list /etc/apt/sources.list.d -newer "$lists" 2>/dev/null | head -1)" ]]; then
    return 0
  fi
  apt_get update -qq
}

# apt_prepare — перед стадией: Lock::Timeout для любого apt на узле, ожидание
# блокировок, и если хостер перезагрузил машину посреди установки пакетов,
# dpkg остаётся прерванным и любой apt-get падает с «dpkg was interrupted» -
# доводим его.
apt_prepare() {
  printf 'DPkg::Lock::Timeout "%s";\n' "$APT_LOCK_TIMEOUT" > /etc/apt/apt.conf.d/90anysda-lock-timeout
  apt_wait_idle
  if [[ -n "$(ls -A /var/lib/dpkg/updates 2>/dev/null)" ]]; then
    svc_say "dpkg был прерван, довожу: dpkg --configure -a"
    DEBIAN_FRONTEND=noninteractive flock -w "$APT_LOCK_TIMEOUT" "$APT_FLOCK" \
      dpkg --configure -a --force-confdef --force-confold
  fi
}
