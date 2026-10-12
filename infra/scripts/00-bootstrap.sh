#!/usr/bin/env bash
# Stage 00 — common base. Runs on every host.
# Idempotent: safe to re-run.

set -euo pipefail

# Env file is passed as $1; source it
[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?HOST_TAG must be set}"
# Перезапуск служб только при смене их входов (infra/lib/anysda-svc.sh): деплой
# кладёт библиотеку рядом со стадией, anysda-restore зовёт стадию из клона репо.
SVC_LIB=$(dirname "$0")/anysda-svc.sh
[[ -f "$SVC_LIB" ]] || SVC_LIB=$(dirname "$0")/../lib/anysda-svc.sh
# shellcheck source=infra/lib/anysda-svc.sh
source "$SVC_LIB"
: "${HY2_DIRECT_PORT:?}" "${HY2_WARP_PORT:?}" "${HY2_MGMT_PORT:?}"

STAMP_DIR=/var/anysda/.stamps
mkdir -p "$STAMP_DIR" /etc/anysda /var/lib/anysda

STAGE='00-bootstrap'

if [[ -f "$STAMP_DIR/$STAGE" ]]; then
  echo "[$HOST_TAG] $STAGE уже применён — перезапускаю только идемпотентные части"
fi

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a   # needrestart: рестартить службы сам, без интерактива

# esm-cache.service (Ubuntu Pro) при каждом apt дёргается к esm.ubuntu.com и,
# когда тот недоступен/медленный, виснет — а apt через needrestart его ждёт,
# из-за чего деплой стопорится. Маскируем: для VPN-ноды кэш ESM не нужен.
systemctl mask --now esm-cache.service >/dev/null 2>&1 || true

# ----------------------------------------------------------------------------
# 0. Автообновления Ubuntu - на паузу до конца деплоя
# ----------------------------------------------------------------------------
# На entry deploy.sh ставит паузу ещё до своих зависимостей (apt_auto_pause
# в lib/anysda-svc.sh); повтор здесь ничего не ждёт и продлевает таймер.
echo "[$HOST_TAG] [0/6] автообновления на паузу до конца деплоя"
apt_auto_pause

# ----------------------------------------------------------------------------
# 1. apt update + base packages
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/6] apt пакеты"
PKGS=(wireguard wireguard-tools
      curl wget ca-certificates gnupg lsb-release
      openssl gettext-base iproute2 procps kmod
      ufw fail2ban
      jq qrencode unzip git
      iptables netcat-openbsd
      python3 python3-yaml)
SKIP=()
# Пакеты стадий ru (29, 19, 22, 26, 27) - сюда же: entry кончает bootstrap
# раньше экзитов и ждёт их, а потом стадии ставили бы своё по одному apt на
# критическом пути (~33 с на чистой). Свои проверки стадии сохраняют.
# Одним вызовом: каждый заход dpkg - свой прогон триггеров (~3 с на ядро).
if [[ "$HOST_TAG" == ru ]]; then
  PKGS+=(openvpn openssl strongswan strongswan-starter strongswan-pki
         strongswan-swanctl libcharon-extra-plugins libstrongswan-extra-plugins
         apache2-utils)
  # zapret2 - чистый C: g++ из build-essential не нужен.
  [[ "${YT_ROUTE:-off}" == zapret ]] && PKGS+=(gcc make libc6-dev libcap-dev
    libnetfilter-queue-dev libnfnetlink-dev libmnl-dev zlib1g-dev
    libluajit-5.1-dev luajit nftables ethtool)
  [[ "${BACKUP_ENABLED:-}" == true ]] && PKGS+=(age expect sqlite3)
  # Docker для 25/30/35 - тем же заходом.
  if ! command -v docker >/dev/null 2>&1; then
    docker_apt_source
    PKGS+=("${DOCKER_PKGS[@]}")
    SKIP+=("${DOCKER_SKIP[@]}")
  fi
fi
# Повторный деплой: всё уже стоит - ни update, ни install. Обновления пакетов
# ставят автообновления, а не деплой.
if ! apt_have "${PKGS[@]}"; then
  apt_update_if_stale
  apt_get install -y -qq "${PKGS[@]}" "${SKIP[@]}" >/dev/null
fi
[[ "$HOST_TAG" != ru ]] || systemctl enable --now docker >/dev/null 2>&1 || true
# Note: iptables-persistent/netfilter-persistent conflict with ufw — we use
# ufw for the firewall and manage routing-specific iptables rules via systemd
# oneshot units (later stages).

# ----------------------------------------------------------------------------
# 2. Sysctl tuning
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [2/6] sysctl"
cat > /etc/sysctl.d/99-anysda.conf <<EOF
# anysda-vpn — set by infra/scripts/00-bootstrap.sh
net.ipv4.ip_forward = 1
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
# AdGuard слушает MGMT_IP (10.99.0.1, lo-алиас из 05-mgmt-mesh), а стадия 22
# может стартовать его раньше, чем алиас поднят после ребута.
# nonlocal_bind разрешает создать сокет на IP, которого ещё нет на интерфейсах.
net.ipv4.ip_nonlocal_bind = 1
# IPv6 left enabled (kernel default); we just don't use it in the app
net.ipv6.conf.all.disable_ipv6 = 0
# QUIC/Hysteria2: крупные UDP-буферы — дефолт ~208 КБ режет пропускную
# способность QUIC; sing-box без них сам ругается в логи.
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
# hy2 с obfs salamander: обёртка сокета прячет SetReadBuffer, и quic-go молча
# остаётся на системном дефолте (~208 КБ), rmem_max ему не помогает. Поэтому
# поднимаем сам дефолт; UDP-пакеты экзита и хаба на 200+ Мбит/с иначе
# теряются в UdpRcvbufErrors. Сокеты берут его при создании: sing-box
# перезапускается по отпечатку в 10-foreign и 20-ru-router, куда входит этот файл.
net.core.rmem_default = 8388608
net.core.wmem_default = 8388608
net.core.netdev_max_backlog = 5000
# BBR + fq для новых интерфейсов; WAN хаба настроен отдельно ниже (IKEv2).
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
# Соединения внутри туннеля живут долго и простаивают: окно после паузы не
# сбрасываем. PMTU ищем зондированием, если ICMP по пути режут. Неотправленное
# в сокете держим коротким, чтобы мелкие ответы не стояли за закачкой.
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_notsent_lowat = 131072
EOF
sysctl --quiet --system

# default_qdisc не меняет уже созданный WAN. На экзитах сохраняем fq с
# flow_limit 1000: один hy2-поток несёт трафик всех клиентов. На WAN хаба
# возвращаем pfifo_fast: fq снижает IKEv2 tcp8_down (VPN2-163). TCP BBR,
# буферы и fq на остальных интерфейсах/экзитах остаются прежними.
WAN_QDISC=fq
[[ "$HOST_TAG" != ru ]] || WAN_QDISC=pfifo_fast
cat > /usr/local/sbin/anysda-fq <<'EOF'
#!/bin/sh
# anysda-vpn — set by infra/scripts/00-bootstrap.sh
set -eu
dev=$(ip -4 route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}')
[ -n "$dev" ] || { echo "anysda-fq: нет маршрута по умолчанию" >&2; exit 1; }
case "${1:?ожидается fq или pfifo_fast}" in
  pfifo_fast)
    tc qdisc show dev "$dev" root | grep -q '^qdisc pfifo_fast ' && exit 0
    tc qdisc replace dev "$dev" root pfifo_fast
    ;;
  fq)
    current=$(tc qdisc show dev "$dev" root)
    if echo "$current" | grep -q '^qdisc fq .*flow_limit 1000p' &&
       ! echo "$current" | grep -q 'nopacing'; then
      exit 0
    fi
    tc qdisc replace dev "$dev" root fq flow_limit 1000 pacing
    ;;
  *) echo "anysda-fq: неизвестная очередь $1" >&2; exit 1 ;;
esac
echo "anysda-fq: $dev -> $1"
EOF
chmod 755 /usr/local/sbin/anysda-fq
cat > /etc/systemd/system/anysda-fq.service <<EOF
# anysda-vpn — set by infra/scripts/00-bootstrap.sh
[Unit]
Description=anysda-vpn: $WAN_QDISC on the default route interface
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/anysda-fq $WAN_QDISC

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable anysda-fq >/dev/null 2>&1
/usr/local/sbin/anysda-fq "$WAN_QDISC" | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

# systemd-networkd при каждом перезапуске (needrestart после unattended-upgrades
# дёргает его сам) по умолчанию сносит «чужие» ip rule и маршруты — в том числе
# fwmark 0x42 → table 101, на которой держится TPROXY клиентов wg0/OpenVPN/IKEv2.
# Без них у всех клиентов пропадает интернет до ребута. Чужое не трогать.
# Перезапускать networkd не нужно: файл читается при следующем старте.
mkdir -p /etc/systemd/networkd.conf.d
cat > /etc/systemd/networkd.conf.d/60-anysda-foreign.conf <<'EOF'
# anysda-vpn — set by infra/scripts/00-bootstrap.sh
[Network]
ManageForeignRoutingPolicyRules=no
ManageForeignRoutes=no
EOF

# ----------------------------------------------------------------------------
# 3. SSH hardening + переход на ключевой вход
#
# Ключи ставим всегда, какие есть: ключ(и) оркестратора (/tmp/anysda/
# orchestrator_keys) — чтобы деплой продолжал ходить, и ключ человека-админа
# (ADMIN_SSH_PUBKEY из config.yaml → all.env).
#
# VPN2-31, entry: парольный вход отключаем ТОЛЬКО когда у ЧЕЛОВЕКА-АДМИНА есть
# ключ. Ключ оркестратора для этого не считается: оркестратор живёт на entry,
# и после её переустановки без админского ключа на неё не зайти ничем (именно
# так залочило NL). Нет админского ключа → пароль остаётся, fail2ban (шаг 5)
# прикрывает брутфорс.
# Экзиты: пароль отключаем всегда, как только в authorized_keys реально лёг
# ключ оркестратора — на экзит ходят с entry по ключу, а пароль на публичном
# 22 только подставляет экзит под перебор. Ключ не лёг → пароль оставляем и
# громко предупреждаем, иначе экзит заперт.
# ----------------------------------------------------------------------------
SSHD=/etc/ssh/sshd_config
ORCH_KEYS=/tmp/anysda/orchestrator_keys
ADMIN_PUBKEY="${ADMIN_SSH_PUBKEY:-}"

# Собираем все ключи, которые надо положить в authorized_keys.
KEYSRC=$(mktemp)
[[ -s "$ORCH_KEYS" ]] && cat "$ORCH_KEYS" >> "$KEYSRC"
[[ -n "$ADMIN_PUBKEY" ]] && printf '%s\n' "$ADMIN_PUBKEY" >> "$KEYSRC"

if [[ -s "$KEYSRC" ]]; then
  mkdir -p /root/.ssh
  chmod 700 /root/.ssh
  touch /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
  # Мерджим: уникальные ключи из существующих + оркестратор + админ.
  cat /root/.ssh/authorized_keys "$KEYSRC" | awk 'NF && !seen[$0]++' > /root/.ssh/authorized_keys.new
  mv /root/.ssh/authorized_keys.new /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
  echo "[$HOST_TAG]   ключей в /root/.ssh/authorized_keys: $(wc -l < /root/.ssh/authorized_keys)"
fi
rm -f "$KEYSRC"

# Ключ entry для ssh через служебный hy2-туннель (VPN2-38; на экзиты его
# кладёт deploy.sh, см. prep_entry_mgmt_key). Опознаём по комментарию:
# прежний ключ entry снимаем, текущий ставим один раз, поэтому повтор
# стадии не плодит строки, а пересозданная entry вытесняет старый ключ.
# Ключ приходит только через hy2-mgmt-in, а он отдаёт соединение sshd с
# 127.0.0.1: from= не пускает этот ключ с публичного адреса экзита.
ENTRY_KEY=/tmp/anysda/entry_mgmt_key
if [[ "$HOST_TAG" != ru && -s "$ENTRY_KEY" ]]; then
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  touch /root/.ssh/authorized_keys
  { awk '$NF != "anysda-entry-mgmt"' /root/.ssh/authorized_keys
    printf 'from="127.0.0.1,::1" %s\n' "$(head -n1 "$ENTRY_KEY")"
  } > /root/.ssh/authorized_keys.new
  mv /root/.ssh/authorized_keys.new /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
  echo "[$HOST_TAG]   ключ entry (anysda-entry-mgmt) в authorized_keys"
fi

# VPN2-47: значения пишем в свой drop-in 00-anysda.conf. sshd берёт ПЕРВОЕ
# встреченное значение, а sshd_config.d/*.conf подключается в начале
# sshd_config: правка только основного файла проигрывала 50-cloud-init.conf
# с PasswordAuthentication yes. 00- идёт раньше 50-, поэтому побеждает.
# Основной файл правим тоже — на случай sshd без Include sshd_config.d.
SSHD_DROPIN=/etc/ssh/sshd_config.d/00-anysda.conf
mkdir -p /etc/ssh/sshd_config.d
write_sshd_auth() {  # $1 = yes|no — парольный вход
  local root_login=prohibit-password
  [[ "$1" == yes ]] && root_login=yes
  printf '%s\n' \
    "# anysda-vpn — set by infra/scripts/00-bootstrap.sh" \
    "PasswordAuthentication $1" \
    "PermitRootLogin $root_login" \
    "KbdInteractiveAuthentication no" > "$SSHD_DROPIN"
  sed -i "s/^#\?PasswordAuthentication.*/PasswordAuthentication $1/" "$SSHD"
  sed -i "s/^#\?PermitRootLogin.*/PermitRootLogin $root_login/" "$SSHD"
}
ORCH_KEY=""
[[ -s "$ORCH_KEYS" ]] && ORCH_KEY=$(awk 'NF {print; exit}' "$ORCH_KEYS")
if [[ "$HOST_TAG" != ru ]]; then
  if [[ -n "$ORCH_KEY" ]] && grep -qxF -- "$ORCH_KEY" /root/.ssh/authorized_keys 2>/dev/null; then
    echo "[$HOST_TAG] [3/6] sshd: ключ оркестратора на месте — отключаю парольный вход на экзите"
    write_sshd_auth no
  else
    echo "[$HOST_TAG] [3/6] ВНИМАНИЕ: ключа оркестратора нет в /root/.ssh/authorized_keys —"
    echo "[$HOST_TAG]       парольный вход на экзите ОСТАВЛЕН, чтобы не запереть ноду (VPN2-31)."
    echo "[$HOST_TAG]       Положи ключ и повтори стадию 00, тогда пароль отключится."
    write_sshd_auth yes
  fi
elif [[ -n "$ADMIN_PUBKEY" ]]; then
  echo "[$HOST_TAG] [3/6] sshd: есть ключ админа — отключаю парольный вход"
  write_sshd_auth no
else
  echo "[$HOST_TAG] [3/6] sshd: ключа админа нет — оставляю PasswordAuthentication=yes (VPN2-31)"
  write_sshd_auth yes
fi
sed -i 's/^#\?ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' "$SSHD"
sed -i 's/^#\?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' "$SSHD"
# OpenSSH 9.8+ (Ubuntu 24.10+ / 26.04) включает PerSourcePenalties: 90с штраф
# за "crashed" коннект. Несколько неудачных sshpass-ов лочат source IP и весь
# деплой встаёт. Отключаем — но только если sshd знает эту директиву
# (на 24.04 / OpenSSH 9.6 её нет, и неизвестная опция роняет sshd).
if sshd -T 2>/dev/null | grep -i '^persourcepenalties ' >/dev/null; then
  printf 'PerSourcePenalties no\n' > /etc/ssh/sshd_config.d/99-anysda-no-penalties.conf
else
  rm -f /etc/ssh/sshd_config.d/99-anysda-no-penalties.conf
fi
if ! sshd -t 2>&1; then
  echo "[$HOST_TAG] конфиг sshd невалиден — откатываюсь"
  # safe fallback: оставляем парольный вход чтобы не запереть себя
  write_sshd_auth yes
  rm -f /etc/ssh/sshd_config.d/99-anysda-no-penalties.conf
  exit 1
fi
systemctl reload ssh

# ----------------------------------------------------------------------------
# 4. ufw firewall — role-specific
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/6] ufw"
# VPN2-41: без `ufw --force reset`. Он стирал порты, которые открывают
# стадии 27/28/29/30 (IKEv2, WireGuard, OpenVPN, HTTPS панели): повторный
# 00-bootstrap отрезал всех клиентов. `ufw allow` на уже существующее правило
# ничего не добавляет, так что повтор стадии идемпотентен.
# На включённом ufw `ufw allow`/`ufw delete` применяются сразу, поэтому стадии
# не зовут `ufw reload`: он, как и `ufw default`, перегружает весь фаервол, и на
# эти доли секунды теряются пакеты туннелей клиентов. Политику меняем, только
# если она другая.
grep -qx 'DEFAULT_INPUT_POLICY="DROP"' /etc/default/ufw 2>/dev/null || ufw default deny incoming
grep -qx 'DEFAULT_OUTPUT_POLICY="ACCEPT"' /etc/default/ufw 2>/dev/null || ufw default allow outgoing
ufw allow 22/tcp comment 'ssh'

case "$HOST_TAG" in
  ru)
    # WG/OpenVPN listener-порты открывают сами стадии 28/29.
    ufw allow 80/tcp            comment 'caddy HTTP панель'
    ;;
  *)
    # Hysteria2 порты — дефолтные. Per-exit override открывается стейджем
    # 10-foreign (он видит финальные значения после rotation).
    ufw allow "${HY2_DIRECT_PORT}/udp" comment 'hysteria2 direct'
    ufw allow "${HY2_WARP_PORT}/udp"   comment 'hysteria2 warp'
    # Служебный hy2-туннель (скрейп node_exporter из-за границы вместо WG-mesh).
    ufw allow "${HY2_MGMT_PORT}/udp"   comment 'hysteria2 mgmt'
    # Caddy-обманки на экзитах нет, а 80/443 tcp открывались под неё:
    # снимаем правила с нод, поставленных раньше.
    ufw delete allow 80/tcp  >/dev/null 2>&1 || true
    ufw delete allow 443/tcp >/dev/null 2>&1 || true
    ;;
esac
ufw --force enable
# VPN2-33: без журнала. Иначе каждый пакет сканера портов — строка
# [UFW BLOCK] в kern.log и журнале, это лишняя запись на медленный диск.
ufw logging off >/dev/null
ufw status verbose | HOST_TAG="$HOST_TAG" awk '{print "[" ENVIRON["HOST_TAG"] "]   " $0}'

# ----------------------------------------------------------------------------
# 5. fail2ban
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [5/6] fail2ban"
cat > /etc/fail2ban/jail.d/anysda.local <<EOF
# anysda-vpn — set by infra/scripts/00-bootstrap.sh
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
port = ssh
backend = systemd
EOF
systemctl enable fail2ban >/dev/null 2>&1
svc_restart_if_changed fail2ban /usr/bin/fail2ban-server /usr/lib/systemd/system/fail2ban.service \
  /etc/fail2ban/jail.d/anysda.local

# ----------------------------------------------------------------------------
# 6. node_exporter — слушает ТОЛЬКО loopback. Раньше стадия 05 перевешивала его
# на mgmt-iface (WG-mesh); теперь скрейп экзитов идёт через служебный hy2-туннель
# и приходит на 127.0.0.1:9100 с самого экзита, поэтому наружу порт не смотрит.
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [6/6] node_exporter"
NE_VERSION='1.12.1'
if [[ ! -x /usr/local/bin/node_exporter ]] || ! /usr/local/bin/node_exporter --version 2>&1 | grep "version $NE_VERSION " >/dev/null; then
  TMP=$(mktemp -d)
  REL_URL="https://github.com/prometheus/node_exporter/releases/download/v${NE_VERSION}"
  ARCHIVE="node_exporter-${NE_VERSION}.linux-amd64.tar.gz"
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    "$REL_URL/$ARCHIVE" -o "$TMP/$ARCHIVE"
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    "$REL_URL/sha256sums.txt" -o "$TMP/sha256sums.txt"
  ( cd "$TMP" && grep -E "[[:space:]]${ARCHIVE}\$" sha256sums.txt | sha256sum -c - ) \
    || { rm -rf "$TMP"; echo "[$HOST_TAG] node_exporter SHA-256 mismatch — abort" >&2; exit 1; }
  tar -xz -C "$TMP" -f "$TMP/$ARCHIVE"
  install -m 0755 "$TMP/node_exporter-${NE_VERSION}.linux-amd64/node_exporter" /usr/local/bin/node_exporter
  rm -rf "$TMP"
fi
id node_exporter >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin node_exporter

cat > /etc/systemd/system/node_exporter.service <<EOF
[Unit]
Description=Prometheus node_exporter (managed by anysda-vpn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=node_exporter
# Слушаем только loopback: entry скребёт локально, экзиты — через hy2-туннель,
# который на самом экзите бьёт в 127.0.0.1:9100. Наружу порт не открываем.
ExecStart=/usr/local/bin/node_exporter --web.listen-address=127.0.0.1:9100
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable node_exporter >/dev/null 2>&1
svc_restart_if_changed node_exporter /usr/local/bin/node_exporter /etc/systemd/system/node_exporter.service

# ----------------------------------------------------------------------------
# Stamp
# ----------------------------------------------------------------------------
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
