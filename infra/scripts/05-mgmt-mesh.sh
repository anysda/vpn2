#!/usr/bin/env bash
# Stage 05 — management WireGuard mesh (10.99.0.0/24) между всеми нодами.
# Поддерживает произвольное количество exit-нод.
# Оркестратор должен запушить: /tmp/anysda/peers.env (pubkeys + endpoints).

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}" "${MGMT_IP:?}" "${MGMT_PORT:?}" "${EXIT_TAGS:?}"

PEERS=/tmp/anysda/peers.env
[[ -f "$PEERS" ]] || { echo "[$HOST_TAG] $PEERS не найден — оркестратор должен был его загрузить"; exit 1; }
source "$PEERS"

# Проверяем наличие pubkey/endpoint для всех нод
for _t in ru $EXIT_TAGS; do
  _T=$(echo "$_t" | tr a-z A-Z)
  eval ": \"\${PUBKEY_${_T}:?pubkey для $_t не найден в peers.env}\""
  eval ": \"\${EP_${_T}:?endpoint для $_t не найден в peers.env}\""
done

STAMP_DIR=/var/anysda/.stamps
STAGE='05-mgmt-mesh'

PRIVKEY=$(cat /etc/wireguard/wgmgmt.privkey)

peer_block() {
  local pubkey="$1" allowed="$2" endpoint="$3"
  cat <<EOF
[Peer]
PublicKey = $pubkey
AllowedIPs = $allowed
Endpoint = $endpoint
PersistentKeepalive = 25

EOF
}

CONF=/etc/wireguard/wgmgmt.conf
{
  # Self — Interface
  cat <<EOF
[Interface]
Address = ${MGMT_IP}/24
ListenPort = ${MGMT_PORT}
PrivateKey = ${PRIVKEY}
MTU = 1380

EOF

  # Peers — все ноды кроме самой себя
  for _t in ru $EXIT_TAGS; do
    [[ "$_t" == "$HOST_TAG" ]] && continue
    _T=$(echo "$_t" | tr a-z A-Z)
    _pubkey_var="PUBKEY_${_T}"
    _ep_var="EP_${_T}"
    _mgmt_var="MGMT_IP_${_T}"
    # MGMT_IP_{T} берётся из all.env (уже в env после source "$1")
    peer_block "${!_pubkey_var}" "${!_mgmt_var}/32" "${!_ep_var}"
  done
} > "$CONF"
chmod 600 "$CONF"
echo "[$HOST_TAG] wgmgmt.conf: interface ${MGMT_IP}/24, $(echo ru $EXIT_TAGS | wc -w | tr -d ' ') нод"

# UFW
ufw allow "$MGMT_PORT/udp" comment 'wg-mgmt listen' >/dev/null 2>&1 || true
ufw allow proto udp from 10.99.0.0/24 to any port "$MGMT_PORT" comment 'wg-mgmt mesh' >/dev/null 2>&1 || true
ufw allow proto tcp from 10.99.0.0/24 to any port 9100 comment 'node_exporter mesh' >/dev/null 2>&1 || true
ufw reload >/dev/null

# Enable + restart
systemctl enable wg-quick@wgmgmt >/dev/null 2>&1
systemctl restart wg-quick@wgmgmt

# Rebind node_exporter на mgmt IP
sed -i "s|--web.listen-address=[0-9.]*:9100|--web.listen-address=${MGMT_IP}:9100|" \
  /etc/systemd/system/node_exporter.service
systemctl daemon-reload
systemctl restart node_exporter

sleep 2
echo "[$HOST_TAG] wg show wgmgmt:"
wg show wgmgmt | sed "s/^/[$HOST_TAG]   /"

mkdir -p "$STAMP_DIR"
touch "$STAMP_DIR/$STAGE"
echo "[$HOST_TAG] $STAGE done"
