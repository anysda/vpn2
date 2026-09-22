#!/usr/bin/env bash
# Stage 05 — служебный слой.
#
# Раньше здесь поднимался WireGuard-mesh (wgmgmt, 10.99.0.0/24), по которому
# entry скребла node_exporter экзитов. WG через границу РФ блокируется, поэтому
# служебный скрейп переехал на отдельный Hysteria2-туннель (см.
# docs/mgmt-over-hysteria2-design.md): экзит держит hy2-mgmt-in (стадия 10),
# entry ходит к нему локальным SOCKS-инбаундом (стадия 20), VictoriaMetrics
# скребёт через него (стадия 25).
#
# Что осталось за этой стадией:
#   • entry: держать 10.99.0.1 алиасом на lo — на этот адрес завязаны локальные
#     потребители (clash-api sing-box, AdGuard, панель, watchdog). Он больше
#     НЕ пересекает границу, просто локальный служебный адрес.
#   • все ноды: снести артефакты старого wgmgmt (идемпотентная миграция) и
#     вернуть node_exporter на loopback (в mesh-эпоху он бинжился на MGMT_IP).
#
# Идемпотентно: safe to re-run.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"

STAMP_DIR=/var/anysda/.stamps
STAGE='05-mgmt-mesh'

# ----------------------------------------------------------------------------
# 1. Снос старого WireGuard-mesh (миграция с mesh-эпохи; на чистой ноде no-op)
# ----------------------------------------------------------------------------
if systemctl list-unit-files 'wg-quick@wgmgmt.service' 2>/dev/null | grep -q wgmgmt \
   || ip link show wgmgmt >/dev/null 2>&1; then
  echo "[$HOST_TAG] снимаю устаревший wgmgmt (mesh переехал на Hysteria2)"
  systemctl disable --now wg-quick@wgmgmt >/dev/null 2>&1 || true
  ip link del wgmgmt >/dev/null 2>&1 || true
  rm -f /etc/wireguard/wgmgmt.conf /etc/wireguard/wgmgmt.privkey
  # старые mesh-правила ufw
  ufw delete allow proto udp from 10.99.0.0/24 to any port "${MGMT_PORT:-51900}" >/dev/null 2>&1 || true
  ufw delete allow proto tcp from 10.99.0.0/24 to any port 9100 >/dev/null 2>&1 || true
fi

# ----------------------------------------------------------------------------
# 2. node_exporter — только loopback (в mesh-эпоху бинжился на MGMT_IP:9100)
# ----------------------------------------------------------------------------
if grep -q -- '--web.listen-address=' /etc/systemd/system/node_exporter.service 2>/dev/null; then
  if ! grep -q -- '--web.listen-address=127.0.0.1:9100' /etc/systemd/system/node_exporter.service; then
    echo "[$HOST_TAG] возвращаю node_exporter на 127.0.0.1:9100"
    sed -i 's|--web.listen-address=[0-9.]*:9100|--web.listen-address=127.0.0.1:9100|' \
      /etc/systemd/system/node_exporter.service
    systemctl daemon-reload
    systemctl restart node_exporter
  fi
fi

# ----------------------------------------------------------------------------
# 3. entry: 10.99.0.1 как алиас на lo (локальный служебный адрес)
# ----------------------------------------------------------------------------
if [[ "$HOST_TAG" == "ru" ]]; then
  MGMT_LOOPBACK="${MGMT_IP:-10.99.0.1}"
  # Persist через systemd oneshot — переживает ребут (без сетевого демона).
  # `ip addr replace` идемпотентен: добавит, если адреса нет, и не упадёт, если
  # он уже висит — поэтому и первый запуск, и повтор чисты (unit не уходит в
  # failed). `enable --now` сразу и вешает адрес.
  cat > /etc/systemd/system/anysda-mgmt-loopback.service <<EOF
[Unit]
Description=anysda-vpn mgmt loopback alias (${MGMT_LOOPBACK})
After=network-pre.target
Before=sing-box.service adguardhome.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/ip addr replace ${MGMT_LOOPBACK}/32 dev lo
ExecStop=/sbin/ip addr del ${MGMT_LOOPBACK}/32 dev lo

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now anysda-mgmt-loopback >/dev/null 2>&1 || true
  echo "[$HOST_TAG] lo:"
  ip -4 -o addr show dev lo | sed "s/^/[$HOST_TAG]   /"
fi

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
