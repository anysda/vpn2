#!/usr/bin/env bash
# Stage 27 — outline-ss-server install on RU node.
# Listens on SS_PORT for client Shadowsocks connections (TCP+UDP).
# Runs as system user 'outline' so stage 20-ru-router can match outbound
# connections via iptables `--uid-owner outline` and redirect them into
# the sing-box redirect/tproxy chain.
#
# The panel (stage 30) writes /etc/outline-ss-server/config.yml with the
# enabled clients' SS keys and sends SIGHUP to this service for hot-reload.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${SS_PORT:?}"

if [[ "$HOST_TAG" != "ru" ]]; then
  echo "[$HOST_TAG] stage 27-shadowsocks: skipped (only RU)"
  exit 0
fi

STAGE='27-shadowsocks'
STAMP_DIR=/var/anysda/.stamps
mkdir -p "$STAMP_DIR" /etc/outline-ss-server

OSS_VERSION='v1.7.3'
OSS_BIN='/usr/local/bin/outline-ss-server'

# 1. Create system user (no login, no home dir)
if ! id outline >/dev/null 2>&1; then
  echo "[$HOST_TAG] [1/4] creating system user 'outline'"
  useradd --system --shell /usr/sbin/nologin --no-create-home --user-group outline
fi

# 2. Download binary
NEED_INSTALL=0
if [[ ! -x "$OSS_BIN" ]]; then
  NEED_INSTALL=1
elif ! "$OSS_BIN" -version 2>&1 | grep -qF "${OSS_VERSION#v}"; then
  # `-version` печатает «1.7.0» без ведущего v — сверяем без него.
  NEED_INSTALL=1
fi

if [[ "$NEED_INSTALL" -eq 1 ]]; then
  echo "[$HOST_TAG] [2/4] installing outline-ss-server $OSS_VERSION"
  TMPDIR=$(mktemp -d)
  trap 'rm -rf "$TMPDIR"' EXIT
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64) ARCH_TAG='linux_x86_64' ;;
    aarch64|arm64) ARCH_TAG='linux_arm64' ;;
    *) echo "Unsupported arch: $ARCH" >&2; exit 1 ;;
  esac
  curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
    -o "$TMPDIR/oss.tar.gz" \
    "https://github.com/Jigsaw-Code/outline-ss-server/releases/download/${OSS_VERSION}/outline-ss-server_${OSS_VERSION#v}_${ARCH_TAG}.tar.gz"
  tar -xzf "$TMPDIR/oss.tar.gz" -C "$TMPDIR"
  install -m 755 -o root -g root "$TMPDIR/outline-ss-server" "$OSS_BIN"
else
  echo "[$HOST_TAG] [2/4] outline-ss-server $OSS_VERSION already installed"
fi

# 3. Initial empty config (the panel will write actual keys via syncShadowsocksConfig + SIGHUP)
if [[ ! -f /etc/outline-ss-server/config.yml ]]; then
  echo "[$HOST_TAG] [3/4] writing initial empty config"
  cat > /etc/outline-ss-server/config.yml <<'EOF'
keys: []
EOF
fi
chown -R outline:outline /etc/outline-ss-server
chmod 0700 /etc/outline-ss-server
chmod 0600 /etc/outline-ss-server/config.yml

# 4. Systemd unit
echo "[$HOST_TAG] [4/4] systemd unit"
cat > /etc/systemd/system/outline-ss-server.service <<EOF
[Unit]
Description=outline-ss-server (anysda-vpn2 Shadowsocks)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=outline
Group=outline
ExecStart=$OSS_BIN -config /etc/outline-ss-server/config.yml -metrics 127.0.0.1:9091 -replay_history 10000
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=2
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/etc/outline-ss-server
ProtectHome=true
PrivateTmp=true
LimitNOFILE=65536
# Только IPv4: outline-ss-server резолвит назначение сам, и Go happy-eyeballs
# периодически выбирал IPv6 — а v6-трафик идёт мимо sing-box (TPROXY/redirect
# только на iptables v4) и виснет. Запрет v6 заставляет ходить по v4 → через
# sing-box → корректный geoip-роутинг и фильтрация.
IPAddressDeny=::/0
IPAddressAllow=0.0.0.0/0

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable outline-ss-server >/dev/null 2>&1 || true
systemctl restart outline-ss-server
sleep 1
systemctl status outline-ss-server --no-pager -n 4 | head -6 | sed "s/^/[$HOST_TAG]   /"

touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
