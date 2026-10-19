#!/usr/bin/env bash
# Stage 23: only the observer runs during normal operation. Everything the
# emergency front needs - image contents, units, packages, certificates - is
# installed here, so activation reaches neither the registry nor package mirrors.
set -euo pipefail
[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"
SVC_LIB=$(dirname "$0")/anysda-svc.sh
[[ -f "$SVC_LIB" ]] || SVC_LIB=$(dirname "$0")/../lib/anysda-svc.sh
# shellcheck source=infra/lib/anysda-svc.sh
source "$SVC_LIB"
RELEASE_LIB=$(dirname "$0")/release.sh
[[ -f "$RELEASE_LIB" ]] || RELEASE_LIB=$(dirname "$0")/../lib/release.sh
# shellcheck source=infra/lib/release.sh
source "$RELEASE_LIB"
CONFIG_SOURCE=${EMERGENCY_CONFIG_SOURCE:-/tmp/anysda/emergency.json}
CONTAINER=vpn2-emergency
test -s "$CONFIG_SOURCE"
python3 -m json.tool "$CONFIG_SOURCE" >/dev/null
PUBLIC_IP=$(python3 - "$CONFIG_SOURCE" <<'PY'
import json
import sys
config = json.load(open(sys.argv[1]))
print(config['nodes'][config['node']]['host'])
PY
)
if command -v ufw >/dev/null; then
  while read -r peer port; do
    ufw allow proto udp from "$peer" to any port "$port" comment 'vpn2 emergency observer'
  done < <(python3 - "$CONFIG_SOURCE" <<'PY'
import json
import sys
config = json.load(open(sys.argv[1]))
for node, address in config['nodes'].items():
    if node != config['node']:
        print(address['host'], config['nodes'][config['node']]['port'])
PY
)
fi

# Docker is only the image store on exits: no bridge and no iptables rules,
# so it never touches the exit firewall or forwarding.
emergency_docker_config() {
  local desired='{"bridge": "none", "iptables": false, "ip6tables": false}'
  install -d -m 0755 /etc/docker
  [[ "$(cat /etc/docker/daemon.json 2>/dev/null)" == "$desired" ]] && return
  printf '%s\n' "$desired" > /etc/docker/daemon.json
  if systemctl is-active --quiet docker.service; then systemctl restart docker.service; fi
}

# Docker as the image store; polkitd lets the sandboxed agent start Hysteria2.
EMERGENCY_PKGS=(qrencode curl openssl nftables polkitd)
EMERGENCY_SKIP=()
[[ "$HOST_TAG" == ru ]] || emergency_docker_config
if ! command -v docker >/dev/null 2>&1; then
  docker_apt_source
  EMERGENCY_PKGS+=("${DOCKER_PKGS[@]}")
  EMERGENCY_SKIP+=("${DOCKER_SKIP[@]}")
fi
if ! apt_have "${EMERGENCY_PKGS[@]}"; then
  apt_update_if_stale
  DEBIAN_FRONTEND=noninteractive apt_get install -y -qq "${EMERGENCY_PKGS[@]}" "${EMERGENCY_SKIP[@]}" >/dev/null
fi

getent passwd vpn2-emergency >/dev/null || useradd --system --home-dir /var/lib/vpn2-emergency --shell /usr/sbin/nologin vpn2-emergency
install -d -m 0700 -o vpn2-emergency -g vpn2-emergency /var/lib/vpn2-emergency
install -d -m 0755 /opt/vpn2-emergency/vpn2_emergency
install -d -m 0750 -o root -g vpn2-emergency /etc/vpn2/emergency
install -m 0640 -o root -g vpn2-emergency "$CONFIG_SOURCE" /etc/vpn2/emergency/agent.json

# The registry is reached only here. The stopped container keeps the image in
# use and shows the installed version in `docker ps -a`.
IMAGE=$(release_image emergency "${EMERGENCY_IMAGE:-}" "${EMERGENCY_IMAGE_PULL:-true}")
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker create --name "$CONTAINER" --network none "$IMAGE" >/dev/null
STAGING=$(mktemp -d /opt/vpn2-emergency/.staging.XXXXXX)
trap 'rm -rf -- "$STAGING"' EXIT
docker cp "$CONTAINER:/opt/vpn2-emergency/." "$STAGING/"
SOURCE_DIR=$STAGING/vpn2_emergency
test -f "$SOURCE_DIR/agent.py"
test -f "$SOURCE_DIR/static/instructions.html"
for module in "$SOURCE_DIR"/*.py; do
  install -m 0644 "$module" /opt/vpn2-emergency/vpn2_emergency/
done
rm -rf -- /opt/vpn2-emergency/vpn2_emergency/static
cp -R "$SOURCE_DIR/static" /opt/vpn2-emergency/vpn2_emergency/static
chmod -R u=rwX,go=rX /opt/vpn2-emergency/vpn2_emergency/static
python3 - "$STAGING/image.json" "$IMAGE" > "$STAGING/release.json" <<'PY'
import json
import sys
release = json.load(open(sys.argv[1]))
print(json.dumps({'version': release['version'], 'revision': release['revision'], 'image': sys.argv[2]}))
PY
install -m 0644 "$STAGING/release.json" /opt/vpn2-emergency/release.json
# Keep exactly the installed image: earlier versions only take disk space.
# The previous one lost its tag to the new pull, and untagged images are
# listed only with --all.
while read -r old; do
  [[ "$old" == "$IMAGE" ]] || docker image rm -f "$old" >/dev/null
done < <(docker images --all --quiet --no-trunc --filter label=vpn2.component=emergency | sort -u)

cat > /etc/systemd/system/vpn2-emergency.service <<'UNIT'
[Unit]
Description=VPN emergency observer
After=network-online.target
Wants=network-online.target
[Service]
User=vpn2-emergency
Group=vpn2-emergency
WorkingDirectory=/opt/vpn2-emergency
ExecStart=/usr/bin/python3 -B -m vpn2_emergency.agent --config /etc/vpn2/emergency/agent.json --state-dir /var/lib/vpn2-emergency
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/vpn2-emergency
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LogRateLimitIntervalSec=60
LogRateLimitBurst=10
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload

if [[ "$HOST_TAG" != ru ]]; then
  # Emergency Hysteria2 waits stopped with its port closed.
  bash "$STAGING/install-hy2.sh" "$PUBLIC_IP" "${EMERGENCY_HY2_PORT:-18443}"
  # Panel certificate in advance; a fallback expiring later is renewed by
  # local openssl at activation, without network access.
  runuser -u vpn2-emergency -- env PYTHONPATH=/opt/vpn2-emergency python3 -B -c '
import sys
from vpn2_emergency import web
web.tls_context(*sys.argv[1:])
' /var/lib/vpn2-emergency "$PUBLIC_IP" /etc/vpn2/tls/ip
fi

systemctl enable vpn2-emergency.service
systemctl restart vpn2-emergency.service
systemctl is-active --quiet vpn2-emergency.service
if [[ "$HOST_TAG" != ru ]]; then
  # The exit needs Docker only during deploy; docker.socket starts it on demand.
  systemctl disable --now docker.service containerd.service >/dev/null 2>&1
  systemctl enable --now docker.socket >/dev/null 2>&1
fi
install -d /var/anysda/.stamps
touch /var/anysda/.stamps/23-emergency
printf '[%s] 23-emergency done: image %s\n' "$HOST_TAG" "$(python3 -c '
import json, sys
r = json.load(open(sys.argv[1]))
print(r["version"], r["revision"][:12])' /opt/vpn2-emergency/release.json)"
