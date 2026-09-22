{
  "log": { "level": "error", "timestamp": true },

  "inbounds": [
    {
      "type": "hysteria2",
      "tag": "hy2-direct-in",
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
    { "type": "direct", "tag": "direct" },
    { "type": "block",  "tag": "block-out" },
    {
      "type": "wireguard",
      "tag": "warp",
      "server": "${WGCF_PEER_ENDPOINT_HOST}",
      "server_port": ${WGCF_PEER_ENDPOINT_PORT},
      "local_address": ["${WGCF_LOCAL_IPV4}/32"],
      "private_key": "${WGCF_PRIVKEY}",
      "peer_public_key": "${WGCF_PEER_PUBKEY}",
      "reserved": ${WGCF_RESERVED_JSON},
      "mtu": 1280
    }
  ],

  "route": {
    "rules": [
      { "inbound": "hy2-direct-in", "outbound": "direct" },
      { "inbound": "hy2-warp-in",   "outbound": "warp"   },
      { "inbound": "hy2-mgmt-in", "ip_cidr": ["127.0.0.1/32"], "port": [9100], "outbound": "direct" },
      { "inbound": "hy2-mgmt-in", "outbound": "block-out" }
    ]
  },

  "experimental": {
    "clash_api": {
      "external_controller": "127.0.0.1:9090",
      "secret": "${CLASH_SECRET}"
    }
  }
}
