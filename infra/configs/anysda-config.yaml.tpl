admin:
  username: ${ADMIN_USER}
  password: ${ADMIN_PASS}
  name: ${ADMIN_USER}
  email: null

wireguard:
  host: ${WG_HOST}
  port: ${WG_PORT}
  ipv4_cidr: ${WG_CLIENT_CIDR}
  dns: [10.8.0.1]
  allowed_ips: [0.0.0.0/0]
  mtu: 1380
  persistent_keepalive: 25
