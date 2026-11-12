"""Входы оракула nodevars: config.yaml по разделам, включая отказы."""
BASE = '''entry:
  host: 203.0.113.10
  password: entrypass
exits:
  - tag: de
    host: 198.51.100.20
    password: exitpass
'''
SECTIONS = {
    'ports': ['ports:\n  hy2_direct: 4443\n  hy2_warp: 8443\n  wireguard: 51820\n  openvpn: 1194\n',
              'ports:\n  wireguard: 70000\n',
              'ports:\n  hy2_hop:\n    enabled: true\n    direct: 20000-20999\n    interval: 5m\n',
              'ports:\n  hy2_hop:\n    direct: 20000-24999\n    warp: 24000-25000\n'],
    'wireguard': ['wireguard:\n  mtu: 1380\n  split_local: yes\n', 'wireguard:\n  mtu: 100\n'],
    'openvpn': ['openvpn:\n  proto: tcp\n', 'openvpn:\n  proto: sctp\n'],
    'failover': ['failover:\n  restart_after_checks: 3\n  restart_min_interval: 600\n'],
    'alerts': ['alerts:\n  cpu_pct: 90\n  ram_pct: 80\n  window: 300\n', 'alerts:\n  window: 10\n',
               'alerts:\n  cpu_pct: 101\n'],
    'admin': ['admin:\n  user: boss\n  password: secret12\n  ssh_pubkey: " ssh-ed25519 AAAA key "\n'],
    'panel': ['panel:\n  domain: Panel.Example.COM.\n  image: ghcr.io/x/panel:1\n',
              'panel:\n  domain: localhost\n'],
    'tgbot': ['tgbot:\n  image: ghcr.io/x/bot:1\n'],
    'telegram': ['telegram:\n  bot_token: "123:abc"\n  chat_id: "42"\n  admin_username: boss\n',
                 'telegram:\n  bot_token: "123:abc"\n'],
    'youtube': ['youtube:\n  route: zapret\n  quic: allow\n  mark: 512\n  offload: none\n  window_scale: 3\n'
                '  strategy: fake\n  zapret_ref: v1\n  domains: a.com\n',
                'youtube:\n  route: other\n', 'youtube:\n  quic: drop\n', 'youtube:\n  window_scale: 15\n'],
    'sso': ['sso:\n  enabled: true\n  base_url: https://auth.example.com/\n  client_secret: s3cret\n',
            'sso:\n  enabled: true\n  discovery_url: https://d/x\n  client_secret: s\n  auto_redirect: no\n'
            '  password_login: n\n  label: Keycloak\n  client_id: me\n  allowed_subs: a,b\n',
            'sso:\n  enabled: true\n', 'sso:\n  enabled: false\n'],
    'backup': ['backup:\n  enabled: true\n  passphrase: pp\n',
               'backup:\n  enabled: true\n  backend: s3\n  passphrase: pp\n  retention: 3\n  schedule: weekly\n'
               '  s3:\n    endpoint: https://s3\n    bucket: b\n    region: r\n    access_key: a\n    secret_key: k\n',
               'backup:\n  enabled: true\n  local:\n    dir: /srv/b\n  passphrase: pp\n',
               'backup:\n  enabled: true\n'],
    'entry_resources': ['entry_resources:\n  panel_memory_mib: 512\n  adaway_enabled: true\n'],
}
EXITS = [
    '  - tag: nl\n    host: exit.example.net\n    password: p2\n    hy2_up_mbps: 100\n    hy2_down_mbps: 200\n'
    '    hy2_insecure: true\n    hy2_direct_port: 5555\n',
    '  - tag: nl\n    host: 198.51.100.30\n    password: p2\n    hy2_up_mbps: 100\n',
    '  - tag: ru\n    host: 198.51.100.30\n    password: p2\n',
    '  - tag: de\n    host: 198.51.100.30\n    password: p2\n',
    '  - tag: nl\n    host: "-bad-"\n    password: p2\n',
    '  - tag: nl\n    host: 198.51.100.30\n    password: ""\n',
    '  - tag: my-exit\n    host: 198.51.100.30\n    password: p2\n    hy2_direct_port: 0\n',
]
ENTRY = ['entry:\n  host: 203.0.113.10\n  password: entrypass\n  public_host: vpn.example.com\n',
         'entry:\n  host: 203.0.113.10\n  password: ""\n',
         'entry:\n  host: 999.1.1\n  password: x\n']


def cases():
    """{имя: (текст config.yaml, состояние)}."""
    out = {'base': (BASE, {}), 'no-exits': ('entry:\n  host: 203.0.113.10\n  password: x\n', {}),
           'pinned': (BASE + EXITS[0], {'mgmt-ips.txt': 'nl 7\n# c\nde 3\n'}),
           'excluded': (BASE + EXITS[0], {'excluded-exits.txt': 'de\n'}),
           'all-excluded': (BASE, {'excluded-exits.txt': 'de\n'}),
           'development': (BASE + SECTIONS['panel'][0] + SECTIONS['tgbot'][0], {})}
    for name, variants in SECTIONS.items():
        for index, text in enumerate(variants):
            out[f'{name}-{index}'] = (BASE + text, {})
    for index, text in enumerate(EXITS):
        out[f'exit-{index}'] = (BASE + text, {})
    for index, text in enumerate(ENTRY):
        out[f'entry-{index}'] = (text + BASE[BASE.index('exits:'):], {})
    return out
