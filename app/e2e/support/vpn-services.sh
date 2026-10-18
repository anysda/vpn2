#!/usr/bin/env bash
# Только одноразовая сетевая namespace контейнера e2e, без сети хоста.
set -euo pipefail
ip link show wg0 >/dev/null 2>&1 || ip link add wg0 type wireguard
ip address replace 10.66.66.1/24 dev wg0
ip link set wg0 up
mkdir -p /var/anysda /etc/swanctl/conf.d
cat > /tmp/openvpn-test.conf <<'EOF'
port 1194
proto udp
dev tun
topology subnet
server 10.67.67.0 255.255.255.0 nopool
ifconfig-pool 10.67.67.240 10.67.67.253 255.255.255.0
ca /etc/openvpn/server/pki/ca.crt
cert /etc/openvpn/server/pki/server.crt
key /etc/openvpn/server/pki/server.key
dh none
tls-crypt /etc/openvpn/server/pki/tls-crypt.key
crl-verify /etc/openvpn/server/pki/crl.pem
client-config-dir /etc/openvpn/server/ccd
management /etc/openvpn/server/mgmt.sock unix
keepalive 10 60
verb 3
EOF
openvpn --config /tmp/openvpn-test.conf --daemon --log /tmp/openvpn-test.log
cat > /etc/swanctl/swanctl.conf <<'EOF'
connections {
  anysda-ikev2 {
    version = 2
    local {
      auth = pubkey
    }
    remote-client {
      auth = eap-mschapv2
      eap_id = %any
    }
    children {
      net {
        local_ts = 0.0.0.0/0
      }
    }
  }
}
pools {
  anysda-ikev2-pool {
    addrs = 10.68.68.0/24
  }
  anysda-ikev2-pool6 {
    addrs = fd68:68::/64
  }
}
include conf.d/*.conf
EOF
/usr/lib/ipsec/charon > /tmp/charon-test.log 2>&1 &
charon_pid=$!
trap 'kill "$charon_pid" 2>/dev/null || true' EXIT
for _ in $(seq 30); do
  if swanctl --list-conns >/dev/null 2>&1; then break; fi
  sleep 1
done
while kill -0 "$charon_pid" 2>/dev/null; do
  python3 /tmp/ikev2-test-sync.py >/tmp/ikev2-test-sync.log 2>&1 || true
  sleep 1
done
