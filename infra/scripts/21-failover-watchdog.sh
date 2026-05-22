#!/usr/bin/env bash
# Stage 21 — failover watchdog на RU-роутере.
#
# Ставит лёгкий сервис anysda-failover-watchdog: он активно опрашивает экзиты
# через clash-api раз в ~2с и сразу переключает selector-группу foreign-best
# на живой экзит с наименьшей задержкой. Мёртвый экзит подтверждается
# до-проверками на месте — failover ~5-7с (см. lib/failover-watchdog.py и
# gen-router-config.py — foreign-best там должен быть type=selector).
#
# Должен идти ПОСЛЕ 20-ru-router: тот пишет /etc/anysda/clash-secret.txt и
# поднимает sing-box с clash-api.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"

if [[ "$HOST_TAG" != "ru" ]]; then
  echo "[$HOST_TAG] stage 21-failover-watchdog: skipped (only RU)"
  exit 0
fi

STAGE='21-failover-watchdog'
STAMP_DIR=/var/anysda/.stamps
mkdir -p "$STAMP_DIR" /etc/anysda

WD_SRC=/tmp/anysda/failover-watchdog.py
WD_BIN=/usr/local/bin/anysda-failover-watchdog
[[ -f "$WD_SRC" ]] || { echo "[$HOST_TAG] $WD_SRC не найден (push из deploy.sh не прошёл)"; exit 1; }

# 1. Бинарь
echo "[$HOST_TAG] [1/3] устанавливаю watchdog"
install -m755 -o root -g root "$WD_SRC" "$WD_BIN"

# clash-api секрет пишет стейдж 20-ru-router
CLASH_SECRET=$(cat /etc/anysda/clash-secret.txt 2>/dev/null || true)
[[ -n "$CLASH_SECRET" ]] || { echo "[$HOST_TAG] /etc/anysda/clash-secret.txt пуст — сначала 20-ru-router"; exit 1; }
MGMT="${MGMT_IP:-10.99.0.1}"

# 2. Env + systemd unit
echo "[$HOST_TAG] [2/3] env + systemd unit"
cat > /etc/anysda/failover-watchdog.env <<EOF
CLASH_API=http://${MGMT}:9090
CLASH_SECRET=${CLASH_SECRET}
WATCH_GROUP=foreign-best
PROBE_URL=http://www.gstatic.com/generate_204
INTERVAL=2
PROBE_TIMEOUT_MS=1500
TOLERANCE_MS=120
DEAD_AFTER=2
CONFIRM_GAP=0.4
LATENCY_HOLD=4
EOF
chmod 600 /etc/anysda/failover-watchdog.env

cat > /etc/systemd/system/anysda-failover-watchdog.service <<'EOF'
[Unit]
Description=anysda-vpn failover watchdog (быстрое переключение экзитов)
After=network-online.target sing-box.service
Wants=sing-box.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/anysda-failover-watchdog
EnvironmentFile=/etc/anysda/failover-watchdog.env
Restart=always
RestartSec=2
DynamicUser=yes
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

# 3. Запуск
echo "[$HOST_TAG] [3/3] запуск"
systemctl daemon-reload
systemctl enable anysda-failover-watchdog >/dev/null 2>&1 || true
systemctl restart anysda-failover-watchdog
sleep 3
systemctl status anysda-failover-watchdog --no-pager -n 5 | head -8 | sed "s/^/[$HOST_TAG]   /"

touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
