#!/usr/bin/env bash
# Stage 00 — common base. Runs on every host.
# Idempotent: safe to re-run.

set -euo pipefail

# Env file is passed as $1; source it
[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?HOST_TAG must be set}"
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
# 1. apt update + base packages
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/6] apt пакеты"
apt-get update -qq
apt-get install -y -qq \
  wireguard wireguard-tools \
  curl wget ca-certificates gnupg lsb-release \
  ufw fail2ban \
  jq qrencode unzip git \
  iptables \
  python3 python3-yaml \
  >/dev/null
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
# AdGuard binds to 10.8.0.1:53, но wg0 поднимается позже (stage 30).
# nonlocal_bind разрешает создать сокет на IP, которого ещё нет на интерфейсах.
net.ipv4.ip_nonlocal_bind = 1
# IPv6 left enabled (kernel default); we just don't use it in the app
net.ipv6.conf.all.disable_ipv6 = 0
# QUIC/Hysteria2: крупные UDP-буферы — дефолт ~208 КБ режет пропускную
# способность QUIC; sing-box без них сам ругается в логи.
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
# BBR + fq — выше throughput TCP (и внутри туннеля, и между нодами).
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
EOF
sysctl --quiet --system

# ----------------------------------------------------------------------------
# 3. SSH hardening + переход на ключевой вход
#
# Ключи ставим всегда, какие есть: ключ(и) оркестратора (/tmp/anysda/
# orchestrator_keys) — чтобы деплой продолжал ходить, и ключ человека-админа
# (ADMIN_SSH_PUBKEY из config.yaml → all.env).
#
# VPN2-31: парольный вход отключаем ТОЛЬКО когда у ЧЕЛОВЕКА-АДМИНА есть ключ.
# Ключ оркестратора для этого не считается. Иначе после ротации/переустановки
# нода запиралась и для админа тоже: пароль отключён, а человеческого ключа на
# ноде нет — залезть нельзя ничем (именно так залочило NL). Нет админского
# ключа → оставляем пароль (fail-open), fail2ban (шаг 5) прикрывает брутфорс.
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

if [[ -n "$ADMIN_PUBKEY" ]]; then
  echo "[$HOST_TAG] [3/6] sshd_config: есть ключ админа — отключаю парольный вход"
  sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD"
  sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' "$SSHD"
else
  echo "[$HOST_TAG] [3/6] sshd_config: ключа админа нет — оставляю PasswordAuthentication=yes (VPN2-31)"
  sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' "$SSHD"
  sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' "$SSHD"
fi
sed -i 's/^#\?ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' "$SSHD"
sed -i 's/^#\?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' "$SSHD"
# OpenSSH 9.8+ (Ubuntu 24.10+ / 26.04) включает PerSourcePenalties: 90с штраф
# за "crashed" коннект. Несколько неудачных sshpass-ов лочат source IP и весь
# деплой встаёт. Отключаем — но только если sshd знает эту директиву
# (на 24.04 / OpenSSH 9.6 её нет, и неизвестная опция роняет sshd).
mkdir -p /etc/ssh/sshd_config.d
if sshd -T 2>/dev/null | grep -qi '^persourcepenalties '; then
  printf 'PerSourcePenalties no\n' > /etc/ssh/sshd_config.d/99-anysda-no-penalties.conf
else
  rm -f /etc/ssh/sshd_config.d/99-anysda-no-penalties.conf
fi
if ! sshd -t 2>&1; then
  echo "[$HOST_TAG] конфиг sshd невалиден — откатываюсь"
  # safe fallback: оставляем парольный вход чтобы не запереть себя
  sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' "$SSHD"
  sed -i 's/^PermitRootLogin prohibit-password/PermitRootLogin yes/' "$SSHD"
  rm -f /etc/ssh/sshd_config.d/99-anysda-no-penalties.conf
  exit 1
fi
systemctl reload ssh

# ----------------------------------------------------------------------------
# 4. ufw firewall — role-specific
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [4/6] ufw"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
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
    ufw allow 80/tcp                   comment 'caddy decoy (HTTP)'
    ufw allow 443/tcp                  comment 'caddy decoy (TLS handshake)'
    ;;
esac
ufw --force enable
ufw status verbose | sed "s/^/[$HOST_TAG]   /"

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
systemctl restart fail2ban

# ----------------------------------------------------------------------------
# 6. node_exporter — слушает ТОЛЬКО loopback. Раньше стадия 05 перевешивала его
# на mgmt-iface (WG-mesh); теперь скрейп экзитов идёт через служебный hy2-туннель
# и приходит на 127.0.0.1:9100 с самого экзита, поэтому наружу порт не смотрит.
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [6/6] node_exporter"
NE_VERSION='1.8.2'
if [[ ! -x /usr/local/bin/node_exporter ]] || ! /usr/local/bin/node_exporter --version 2>&1 | grep -q "$NE_VERSION"; then
  curl -sSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    "https://github.com/prometheus/node_exporter/releases/download/v${NE_VERSION}/node_exporter-${NE_VERSION}.linux-amd64.tar.gz" \
    | tar -xz -C /tmp
  install -m 0755 "/tmp/node_exporter-${NE_VERSION}.linux-amd64/node_exporter" /usr/local/bin/node_exporter
  rm -rf "/tmp/node_exporter-${NE_VERSION}.linux-amd64"
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
systemctl restart node_exporter

# ----------------------------------------------------------------------------
# Stamp
# ----------------------------------------------------------------------------
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
