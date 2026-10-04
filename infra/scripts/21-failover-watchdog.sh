#!/usr/bin/env bash
# Stage 21 — failover watchdog на RU-роутере.
#
# Ставит лёгкий сервис anysda-failover-watchdog: он активно опрашивает экзиты
# через clash-api раз в ~2с и сразу переключает selector-группу foreign-best
# на живой экзит с наименьшей задержкой. Мёртвый экзит подтверждается
# до-проверками на месте — failover ~5-7с (см. lib/failover-watchdog.py и
# gen-router-config.py — foreign-best там должен быть type=selector).
#
# Рядом ставит таймер anysda-tproxy-watchdog: возвращает снесённую
# маршрутизацию TPROXY и перезапускает sing-box, если тот перестал читать UDP.
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
echo "[$HOST_TAG] [1/4] устанавливаю watchdog"
install -m755 -o root -g root "$WD_SRC" "$WD_BIN"

# clash-api секрет пишет стейдж 20-ru-router
CLASH_SECRET=$(cat /etc/anysda/clash-secret.txt 2>/dev/null || true)
[[ -n "$CLASH_SECRET" ]] || { echo "[$HOST_TAG] /etc/anysda/clash-secret.txt пуст — сначала 20-ru-router"; exit 1; }
MGMT="${MGMT_IP:-10.99.0.1}"

# 2. Env + systemd unit
echo "[$HOST_TAG] [2/4] env + systemd unit"
cat > /etc/anysda/failover-watchdog.env <<EOF
CLASH_API=http://${MGMT}:9090
CLASH_SECRET=${CLASH_SECRET}
WATCH_GROUP=foreign-best
PROBE_URL=http://www.gstatic.com/generate_204
INTERVAL=2
PROBE_TIMEOUT_MS=3000
TOLERANCE_MS=120
DEAD_AFTER=3
CONFIRM_GAP=0.4
LATENCY_HOLD=4
COOLDOWN_S=60
MAX_COOLDOWN_S=600
PENALTY_RESET_S=300
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
echo "[$HOST_TAG] [3/4] запуск"
systemctl daemon-reload
systemctl enable anysda-failover-watchdog >/dev/null 2>&1 || true
systemctl restart anysda-failover-watchdog
sleep 3
systemctl status anysda-failover-watchdog --no-pager -n 5 | sed -n "1,8s/^/[$HOST_TAG]   /p"

# 4. Сторож TPROXY: раз в минуту проверяет то, без чего клиенты wg0/OpenVPN/
# IKEv2 молча теряют интернет, и чинит на месте.
echo "[$HOST_TAG] [4/4] сторож TPROXY"
cat > /usr/local/sbin/anysda-tproxy-watchdog.sh <<'WDEOF'
#!/usr/bin/env bash
# Сторож TPROXY на entry (anysda-tproxy-watchdog.timer, раз в минуту).
#
# 1. Маршрутизация fwmark 0x42 → table 101 → local default. Её ставят
#    anysda-{wg,ovpn,ikev2}-routing один раз при старте; перезапуск
#    systemd-networkd её стирает, TPROXY метит пакеты, а доставить их в
#    sing-box некуда. Возвращаем на место.
# 2. sing-box 1.10 перестаёт вычитывать UDP-сокет :7898: единственный цикл
#    приёма встаёт на блокирующей отправке в udpnat одной зависшей сессии
#    (дамп 04-10-2026: chan send 439 мин), очередь сокета стоит полной, весь
#    UDP клиентов теряется, TCP при этом работает. Ловим по Recv-Q, SIGQUIT
#    кладёт дамп горутин в журнал, systemd поднимает sing-box (Restart=always).
set -uo pipefail
MARK='0x42'
TABLE=101
PORT=7898
STUCK_BYTES=65536

if iptables -t mangle -S PREROUTING 2>/dev/null | grep -E -- '-j ANYSDA_[A-Z0-9]+_TPROXY' >/dev/null; then
  if ! ip rule list | grep "fwmark $MARK lookup $TABLE" >/dev/null; then
    ip rule add fwmark "$MARK" lookup "$TABLE" \
      && echo "правило fwmark $MARK lookup $TABLE пропало, вернул"
  fi
  if ! ip route show table "$TABLE" 2>/dev/null | grep 'local default' >/dev/null; then
    ip route add local 0.0.0.0/0 dev lo table "$TABLE" \
      && echo "маршрут local default в таблице $TABLE пропал, вернул"
  fi
fi

systemctl is-active --quiet sing-box || exit 0

recvq() {
  ss -H -uln "sport = :$PORT" 2>/dev/null | awk '!f && $4 ~ /^127\.0\.0\.1:/ {print $2; f=1}'
}

# Живой sing-box вычитывает сокет за миллисекунды: три замера подряд за 10 с
# с полной очередью бывают только у зависшего.
samples=()
for i in 1 2 3; do
  q=$(recvq)
  [[ "${q:-0}" -ge "$STUCK_BYTES" ]] || exit 0
  samples+=("$q")
  [[ $i -lt 3 ]] && sleep 5
done

pid=$(systemctl show -p MainPID --value sing-box)
echo "UDP-сокет :$PORT не вычитывается (Recv-Q ${samples[*]}), дамп горутин и перезапуск sing-box"
kill -QUIT "$pid"
for _ in $(seq 1 15); do
  sleep 1
  new=$(systemctl show -p MainPID --value sing-box)
  if [[ "$new" != 0 && "$new" != "$pid" ]] && systemctl is-active --quiet sing-box; then
    echo "sing-box поднят заново (pid $new)"
    exit 0
  fi
done
systemctl restart sing-box
echo "sing-box не поднялся сам за 15 с, перезапущен"
WDEOF
chmod 755 /usr/local/sbin/anysda-tproxy-watchdog.sh

cat > /etc/systemd/system/anysda-tproxy-watchdog.service <<'EOF'
[Unit]
Description=anysda-vpn TPROXY watchdog (маршрутизация 0x42 и UDP-сокет sing-box)
After=sing-box.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/anysda-tproxy-watchdog.sh
EOF

cat > /etc/systemd/system/anysda-tproxy-watchdog.timer <<'EOF'
[Unit]
Description=anysda-vpn TPROXY watchdog, раз в минуту

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now anysda-tproxy-watchdog.timer >/dev/null 2>&1
systemctl is-active anysda-tproxy-watchdog.timer | sed "s/^/[$HOST_TAG]   tproxy watchdog timer: /"

touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
