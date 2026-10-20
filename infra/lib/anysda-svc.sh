# shellcheck shell=bash
# Общие приёмы стадий деплоя и anysda-restore: не рвать клиентов зря и не
# стоять молча на блокировке apt.
#
# Рестарт службы обрывает сессии: IKEv2-устройства после рестарта strongswan
# сами не переподключаются, у WireGuard обнуляются рукопожатия. Поэтому
# службу перезапускаем, только если она читает уже не то, с чем стартовала,
# тем же отпечатком, что у sing-box в 10-foreign и 20-ru-router.

SVC_FP_DIR=${SVC_FP_DIR:-/var/lib/anysda/applied}

# Loads lazily: backup/restore also use this library without release artifacts.
release_download() {
  local name="$1" destination="$2" url="$3" lib
  if [[ "${VPN2_DEVELOPMENT:-false}" == true ]]; then
    curl -fsSL --retry 5 --connect-timeout 20 --max-time 300 -o "$destination" "$url"
  else
    lib=$(dirname "${BASH_SOURCE[0]}")/release.py
    python3 "$lib" --manifest "${VPN2_RELEASE_MANIFEST:-/tmp/anysda/release-manifest.json}" \
      artifact "$name" "$destination" --url "$url"
  fi
}

UFW_RULES_FILE=$(dirname "${BASH_SOURCE[0]:-$0}")/ufw-rules.py
ufw_sync_rules() {
  python3 "$UFW_RULES_FILE" "$@"
}

ufw_sync_dns() {
  local comment=$1 destination=$2 subnet
  shift 2
  local rules=()
  for subnet in "$@"; do
    rules+=("proto udp from $subnet to $destination port 53"
            "proto tcp from $subnet to $destination port 53")
  done
  ufw_sync_rules "$comment" "${rules[@]}"
}

# Единственный список пулов общий с панелью. При деплое файл лежит рядом
# с библиотекой; при restore — в app/shared исходного дерева.
VPN_POOLS_FILE=$(dirname "${BASH_SOURCE[0]:-$0}")/vpn-pools.json
[[ -f "$VPN_POOLS_FILE" ]] || VPN_POOLS_FILE=$(dirname "${BASH_SOURCE[0]:-$0}")/../../app/shared/vpn-pools.json
vpn_pool_subnets() {
  python3 -c 'import json, sys; p=json.load(open(sys.argv[1])); print(" ".join(n for k in sys.argv[2:] for n in p[k]["subnets"]))' "$VPN_POOLS_FILE" "$@"
}

svc_say() { echo "[${HOST_TAG:-anysda}]   $*"; }

# bcrypt ограничен 72 байтами. Короткие пароли совместимы со старыми
# установками; длинные преобразуем целиком, без усечения UTF-8 или суффикса.
# Этот же пароль передаёт AdGuard прокси панели (стадия 30).
agh_password() {
  printf '%s' "$1" | python3 -c 'import hashlib, sys
p = sys.stdin.buffer.read()
sys.stdout.buffer.write(p if len(p) <= 72 else hashlib.sha256(b"vpn2-adguard-v1\0" + p).hexdigest().encode())'
}

# nftables — не единственный владелец ruleset: ufw, Docker, fail2ban и
# routing-службы держат в нём свои таблицы. Даже ExecStop штатного юнита
# делает глобальный flush (restart = stop + start). Убираем оба сброса;
# native nft-конфиги должны заменять только собственные таблицы, как YouTube.
# Вызывается ДО apt/needrestart и после установки пакета на чистой ноде.
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

# Активный oneshot и прежний отпечаток не доказывают наличие правил в ядре.
# При миграции после старого flush перезапускаем только routing, сохраняя
# Environment юнита (MSS и все пулы WG), без рестарта VPN-туннеля.
vpn_routing_ensure() {
  local unit=$1 iface=$2 chain=$3
  if ! iptables -t mangle -C PREROUTING -i "$iface" -j "$chain" 2>/dev/null; then
    svc_say "$unit: цепочка $chain пропала, восстанавливаю"
    systemctl restart "$unit"
  fi
}

# При обновлении уже пострадавшей установки enabled-флаг ufw остался на
# диске, но цепочек в ядре нет. Вернуть также динамические правила служб:
# живой Docker не замечает исчезновения NAT, fail2ban помнит баны в памяти.
firewall_repair_if_lost() {
  local unit
  if grep -qx 'ENABLED=yes' /etc/ufw/ufw.conf 2>/dev/null \
     && ! iptables -S ufw-user-input >/dev/null 2>&1; then
    svc_say 'правила ufw пропали, восстанавливаю firewall и активные службы'
    ufw reload || return
    for unit in docker fail2ban; do
      if systemctl is-active --quiet "$unit"; then
        systemctl restart "$unit" || return
      fi
    done
  fi
}

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

# Входы openvpn-server@server. Его перезапускают 29-openvpn и смена UID
# management в 30-frontend: отпечаток один, иначе следующий деплой видит
# «изменение» и рвёт всех OpenVPN-клиентов. CRL и ccd OpenVPN перечитывает сам
# на каждом подключении, их в отпечатке нет.
ovpn_restart_if_changed() {
  local dir=${OVPN_DIR:-/etc/openvpn/server}
  svc_restart_if_changed openvpn-server@server /usr/sbin/openvpn \
    /usr/lib/systemd/system/openvpn-server@.service "$dir/server.conf" \
    "$dir/pki/ca.crt" "$dir/pki/server.crt" "$dir/pki/server.key" "$dir/pki/tls-crypt.key"
}

# svc_settle SEC [ПРОБА...] — дать только что перезапущенной службе подняться
# (или упасть) до показа статуса. С пробой ждём, пока она пройдёт, но не
# дольше SEC; без пробы — ровно SEC. Если ничего не перезапускали, ждать нечего.
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

# svc_forget UNIT — вход службы поменялся так, что отпечаток файлов этого не
# заметит. Следующий svc_restart_if_changed её перезапустит.
svc_forget() { rm -f "$SVC_FP_DIR/$1.sha256"; }

# wg0_apply — применить /etc/wireguard/wg0.conf. Ключи и пиры `wg syncconf`
# меняет на живом интерфейсе без обрыва. Рестарт wg-quick нужен, только если
# интерфейса нет или в wg0.conf другие адреса. Порт меняет сам syncconf.
wg0_conf_iface() {
  awk -F= '
    /^\[/ { sec = $0; next }
    sec == "[Interface]" {
      k = $1; v = $2; gsub(/[ \t]/, "", k); gsub(/[ \t]/, "", v)
      if (k == "Address") { n = split(v, a, ","); for (i = 1; i <= n; i++) print "addr " a[i] }
    }' /etc/wireguard/wg0.conf | sort
}
wg0_live_iface() {
  ip -o addr show dev wg0 scope global | awk '{print "addr " $4}' | sort
}
wg0_apply() {
  if systemctl is-active --quiet wg-quick@wg0 && ip link show wg0 >/dev/null 2>&1 \
     && [[ "$(wg0_conf_iface)" == "$(wg0_live_iface)" ]]; then
    wg syncconf wg0 <(wg-quick strip wg0) || return
    svc_say "адреса wg0 прежние — wg syncconf без рестарта"
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

# В минимальном Debian нет psmisc/fuser. lslocks из util-linux видит те же
# блокировки ядра; без него отсутствие fuser выглядело как свободный apt.
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
  # mask --now сначала перечитывает замаскированный юнит:
  # активный таймер теряет Trigger и переходит в failed до остановки.
  # Сначала остановим таймеры; reset-failed убирает след старого порядка
  # при повторной установке, не затрагивая ошибки самих APT-служб.
  systemctl stop "${timers[@]}" >/dev/null 2>&1 || true
  systemctl mask --runtime "${timers[@]}" >/dev/null 2>&1 || true
  systemctl reset-failed "${timers[@]}" >/dev/null 2>&1 || true
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
      if [[ "$(systemctl show -P ActiveState "$j" 2>/dev/null)" == @(activating|active|deactivating) ]]; then
        printf '%s ' "$j"
      fi
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
# стартом закрывает штатный DPkg::Lock::Timeout.
apt_get() {
  apt_wait_idle
  apt-get -o DPkg::Lock::Timeout="$APT_LOCK_TIMEOUT" "$@"
}

# deb_install FILE — локальный .deb. Без зависимостей - сразу dpkg -i: apt-get
# install ./x.deb сначала читает все списки (~3 с на ядро). С зависимостями -
# через apt. Перед ним то же ожидание блокировок, что у apt_get.
deb_install() {
  if [[ -n "$(dpkg-deb -f "$1" Depends Pre-Depends 2>/dev/null)" ]]; then
    apt_get install -y -qq "$1"
    return
  fi
  apt_wait_idle
  DEBIAN_FRONTEND=noninteractive dpkg --force-confdef --force-confold -i "$1"
}

# Caddy: панель entry (30) и выпуск сертификата на IP экзита (23).
# APT-репо Caddy на Cloudsmith отвечает 402 Payment Required: apt-get update
# с ним падает целиком. Ставим .deb из GitHub-релиза (тот же пакет, с юнитом и
# пользователем caddy). Список Cloudsmith со старых нод убирает deploy.sh
# перед каждой стадией.
# Ставим и поверх старой версии: проверка «caddy есть» оставляла узлы на той,
# что встала первой.
CADDY_VER='2.11.7'
caddy_install() {
  local tmp rel_url deb
  [[ "$(caddy version 2>/dev/null | cut -d' ' -f1)" != "v${CADDY_VER}" ]] || return 0
  tmp=$(mktemp -d)
  rel_url="https://github.com/caddyserver/caddy/releases/download/v${CADDY_VER}"
  deb="caddy_${CADDY_VER}_linux_amd64.deb"
  release_download caddy "$tmp/$deb" "$rel_url/$deb" || { rm -rf "$tmp"; return 1; }
  if [[ "${VPN2_DEVELOPMENT:-false}" == true ]]; then
    curl -fsSL --retry 5 --connect-timeout 20 "$rel_url/caddy_${CADDY_VER}_checksums.txt" -o "$tmp/checksums.txt"
    ( cd "$tmp" && grep -E "[[:space:]]${deb}\$" checksums.txt | sha512sum -c - ) \
      || { rm -rf "$tmp"; svc_say "caddy SHA-512 mismatch — abort" >&2; return 1; }
  fi
  deb_install "$tmp/$deb" >/dev/null || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
}

# apt_have PKG... — все пакеты уже установлены. apt-get install поверх
# установленного всё равно читает списки и кэш (~2-3 с на ядро).
apt_have() {
  local st
  st=$(dpkg-query -W -f='${db:Status-Status}\n' "$@" 2>/dev/null) || return 1
  ! grep -qvx installed <<<"$st"
}

# docker_apt_source — официальное APT-репо Docker (ключ download.docker.com),
# а не `curl get.docker.com | sh`. Файл
# источника переписываем только при отличии: иначе apt_update_if_stale
# решит, что списки устарели. Docker нужен только entry (25/30/35).
# shellcheck disable=SC2034  # читают 00-bootstrap и 25-monitoring
# buildx/compose на узле не зовёт никто (образы собирает CI), а их и rootless-extras
# тянут Recommends docker-ce/docker-ce-cli: минус ~130 МБ распаковки на 1 ядре.
DOCKER_PKGS=(docker-ce docker-ce-cli containerd.io)
# shellcheck disable=SC2034
DOCKER_SKIP=(docker-ce-rootless-extras- docker-buildx-plugin- docker-compose-plugin-)
docker_apt_source() {
  local list=/etc/apt/sources.list.d/docker.list line distro codename release
  # Debian и Ubuntu имеют разные репозитории, включая ключ. Не подставлять
  # Ubuntu для неизвестной ОС: это создаёт несуществующий suite в APT.
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

# apt_lists_are_fresh LISTS SOURCE... — списки свежие, если каталог обновлён
# за последний час, ни один источник не новее его и в нём есть индекс
# (*_Packages* или *_InRelease). Время индекса не годится: apt ставит файлу
# Last-Modified зеркала, сразу после update он бывает старше месяца. Свежий
# каталог на только что поднятой машине может держать один partial/.
apt_lists_are_fresh() {
  local lists=$1
  shift
  [[ -n "$(find "$lists" -maxdepth 0 -mmin -60 2>/dev/null)" ]] || return 1
  [[ -z "$(find "$@" -newer "$lists" -print -quit 2>/dev/null)" ]] || return 1
  [[ -n "$(find "$lists" -maxdepth 1 -type f \( -name '*_Packages*' -o -name '*_InRelease' \) -print -quit 2>/dev/null)" ]]
}

# apt_update_if_stale — apt-get update, если нет свежего индекса или источник
# менялся после него. После 00-bootstrap свежий индекс пропускает повторный
# update; пустой lists/partial всегда его запускает.
apt_update_if_stale() {
  local lists=/var/lib/apt/lists
  if apt_lists_are_fresh "$lists" /etc/apt/sources.list /etc/apt/sources.list.d; then
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
    DEBIAN_FRONTEND=noninteractive dpkg --configure -a --force-confdef --force-confold
  fi
}
