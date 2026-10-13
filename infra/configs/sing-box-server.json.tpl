{
  "log": { "level": "error", "timestamp": true },

  "inbounds": [
    {
      "type": "trojan",
      "tag": "mux-direct-in",
      "listen": "127.0.0.1",
      "listen_port": 10443,
      "users": [{ "password": "${HY2_PWD_DIRECT}" }],
      "multiplex": { "enabled": true }
    },
    {
      "type": "trojan",
      "tag": "mux-warp-in",
      "listen": "127.0.0.1",
      "listen_port": 10444,
      "users": [{ "password": "${HY2_PWD_WARP}" }],
      "multiplex": { "enabled": true }
    },
    {
      "type": "hysteria2",
      "tag": "hy2-direct-in",
      ${HY2_BANDWIDTH_FIELDS}
      "listen": "::",
      "listen_port": ${HY2_DIRECT_PORT},
      "users": [{ "password": "${HY2_PWD_DIRECT}" }],
      "obfs": { "type": "salamander", "password": "${HY2_OBFS_PWD}" },
      "tls": {
        "enabled": true,
        "certificate_path": "/etc/sing-box/tls.crt",
        "key_path": "/etc/sing-box/tls.key"
      }
    },
    {
      "type": "hysteria2",
      "tag": "hy2-warp-in",
      ${HY2_BANDWIDTH_FIELDS}
      "listen": "::",
      "listen_port": ${HY2_WARP_PORT},
      "users": [{ "password": "${HY2_PWD_WARP}" }],
      "obfs": { "type": "salamander", "password": "${HY2_OBFS_PWD}" },
      "tls": {
        "enabled": true,
        "certificate_path": "/etc/sing-box/tls.crt",
        "key_path": "/etc/sing-box/tls.key"
      }
    },
    {
      "type": "hysteria2",
      "tag": "hy2-mgmt-in",
      "listen": "::",
      "listen_port": ${HY2_MGMT_PORT},
      "users": [{ "password": "${HY2_PWD_MGMT}" }],
      "obfs": { "type": "salamander", "password": "${HY2_OBFS_PWD}" },
      "tls": {
        "enabled": true,
        "certificate_path": "/etc/sing-box/tls.crt",
        "key_path": "/etc/sing-box/tls.key"
      }
    }
  ],

  "outbounds": [
    { "type": "direct", "tag": "direct" }
  ],

  "endpoints": [
    {
      "type": "wireguard",
      "tag": "warp",
      "address": ["${WGCF_LOCAL_IPV4}/32"],
      "private_key": "${WGCF_PRIVKEY}",
      "peers": [
        {
          "address": "${WGCF_PEER_ENDPOINT_HOST}",
          "port": ${WGCF_PEER_ENDPOINT_PORT},
          "public_key": "${WGCF_PEER_PUBKEY}",
          "allowed_ips": ["0.0.0.0/0", "::/0"],
          "reserved": ${WGCF_RESERVED_JSON}
        }
      ],
      "mtu": 1280
    }
  ],

  "route": {
    "rules": [
      { "inbound": "hy2-direct-in", "ip_cidr": ["127.0.0.1/32"], "port": [10443], "network": "tcp", "outbound": "direct" },
      { "inbound": "hy2-warp-in", "ip_cidr": ["127.0.0.1/32"], "port": [10444], "network": "tcp", "outbound": "direct" },
      { "inbound": ["hy2-direct-in", "mux-direct-in"], "action": "resolve" },
      { "inbound": ["hy2-direct-in", "mux-direct-in"], "ip_cidr": ["127.0.0.0/8", "::1/128", "0.0.0.0/32", "::/128"], "action": "reject" },
      { "inbound": ["hy2-direct-in", "mux-direct-in"], "outbound": "direct" },
      { "inbound": ["hy2-warp-in", "mux-warp-in"], "outbound": "warp" },
      { "inbound": "hy2-mgmt-in", "ip_cidr": ["127.0.0.1/32"], "port": [9100, 22], "outbound": "direct" },
      { "inbound": "hy2-mgmt-in", "action": "reject" }
    ]
  },

  "experimental": {
    "clash_api": {
      "external_controller": "127.0.0.1:9090",
      "secret": "${CLASH_SECRET}"
    }
  }
}
