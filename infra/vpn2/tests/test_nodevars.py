"""Переменные узла из пакета (vpn2.nodevars) равны env-файлам config2env.py ключ в ключ."""
import shutil
import tempfile
from pathlib import Path

import pytest

import samples
from nodevars_support import differences, layout, old_env
from vpn2 import config, nodevars

MINIMAL = 'entry:\n  host: 198.18.0.1\n  password: p\nexits:\n  - tag: nl\n    host: 198.18.0.2\n    password: q\n'
OLD_FORMAT = ('entry:\n  host: 198.18.0.1\n  password: p\nexits:\n- tag: nl\n  host: 198.18.0.2\n  password: q\n'
              'admin:\n  user: anysda\npanel:\n  image: registry.example/panel:1.0.0\n'
              'tgbot:\n  image: registry.example/bot:1.0.0\n')
EXITS3 = ('entry:\n  host: 198.18.0.1\n  password: p\nexits:\n'
          '  - tag: nl\n    host: 198.18.0.2\n    password: q\n'
          '  - tag: de-2\n    host: de.example.org\n    password: "w x"\n    hy2_direct_port: 4443\n'
          '    hy2_up_mbps: 100\n    hy2_down_mbps: 200\n    hy2_insecure: true\n'
          '  - tag: us\n    host: 198.18.0.4\n    password: e\n    hy2_direct_port: 443\n    hy2_insecure: false\n')
FULL = EXITS3 + '''ports:
  hy2_direct: 443
  hy2_warp: 8443
  hy2_mgmt: 8444
  wireguard: 051820
  openvpn: 1194
  hy2_hop:
    enabled: false
    direct: 20000-20999
    warp: 21000-21999
    mgmt: 22000-22999
    interval: 30s
wireguard:
  mtu: 1380
  split_local: yes
openvpn:
  proto: tcp
failover:
  restart_after_checks: 3
  restart_min_interval: 600
alerts:
  cpu_pct: 90
  ram_pct: 85
  window: 120
admin:
  user: boss
  password: "pa ss"
  ssh_pubkey: ssh-ed25519 AAAAC3Nza admin@host
panel:
  domain: VPN.Example.ORG.
  image: registry.example/panel:dev
tgbot:
  image: registry.example/bot:dev
telegram:
  bot_token: "123:ABC"
  chat_id: "-100500"
  admin_username: boss
youtube:
  route: Zapret
  quic: ALLOW
  mark: 512
  offload: Fix
  strategy: --dpi-desync=fake --dpi-desync-ttl=3
  zapret_ref: v70
  domains: youtube.com googlevideo.com
  window_scale: native
sso:
  enabled: true
  base_url: https://auth.example.org/
  app_slug: vpn
  client_id: cid
  client_secret: s3cret
  allowed_subs: a,b
  auto_redirect: false
  password_login: false
  label: Вход
backup:
  enabled: true
  backend: s3
  passphrase: "pass phrase"
  retention: 7
  schedule: weekly
  local:
    dir: /srv/backups
  s3:
    endpoint: https://s3.example.org
    bucket: b
    region: ru-1
    access_key: AK
    secret_key: SK
entry_resources:
  panel_memory_mib: 512
  adaway_enabled: false
  retention_days: 30
'''
ODD = r'''entry:
  host: 198.18.0.1
  password: "p'a\"s $HOME `id` \\ ю {{ x }} #1"
  public_host: vpn.example.org
exits:
  - tag: nl
    host: 198.18.0.2
    password: 'it''s $1 "q" %d'
admin:
  user:
  password: '  lead and trail  '
telegram:
  bot_token: "1:a'b"
  chat_id: "5"
sso:
  enabled: true
  client_secret: "se'cr$et"
  discovery_url: https://idp.example.org/.well-known/openid-configuration
backup:
  enabled: yes
  passphrase: "a b'c"
'''
MATRIX = {
    'minimal': (MINIMAL, {}),
    'old-format': (OLD_FORMAT, {}),
    'old-format-development': (OLD_FORMAT, {'development': True}),
    'several-exits': (EXITS3, {}),
    'full': (FULL, {}),
    'full-development': (FULL, {'development': True}),
    'odd-values': (ODD, {}),
    'domain-no-public-host': (MINIMAL + 'panel:\n  domain: vpn.example.org\n', {}),
    'public-host-over-domain': (MINIMAL.replace('  password: p\n', '  password: p\n  public_host: gw.example.org\n')
                                + 'panel:\n  domain: vpn.example.org\n', {}),
    'bot-without-chat': (MINIMAL + 'telegram:\n  bot_token: "1:a"\n', {}),
    'bot': (MINIMAL + 'telegram:\n  bot_token: "1:a"\n  chat_id: "7"\n', {}),
    'sso-requested-no-secret': (MINIMAL + 'sso:\n  enabled: true\n  base_url: https://a.example.org\n', {}),
    'sso-off-with-secret': (MINIMAL + 'sso:\n  enabled: false\n  client_secret: x\n', {}),
    'sso-defaults': (MINIMAL + 'sso:\n  enabled: true\n  client_secret: x\n', {}),
    'backup-off': (MINIMAL + 'backup:\n  enabled: false\n  passphrase: x\n', {}),
    'backup-local-defaults': (MINIMAL + 'backup:\n  enabled: true\n  passphrase: x\n', {}),
    'backup-s3-empty': (MINIMAL + 'backup:\n  enabled: true\n  backend: s3\n  passphrase: x\n  s3:\n    bucket: b\n', {}),
    'hop-empty-block': (MINIMAL + 'ports:\n  hy2_hop:\n', {}),
    'hop-enabled': (MINIMAL + 'ports:\n  hy2_direct: 4443\n  hy2_hop:\n    enabled: true\n', {}),
    'youtube-empty-values': (MINIMAL + 'youtube:\n  route:\n  quic:\n  mark:\n  offload:\n', {}),
    'youtube-direct': (MINIMAL + 'youtube:\n  route: direct\n  window_scale: 7\n', {}),
    'split-local-off': (MINIMAL + 'wireguard:\n  split_local: false\n', {}),
    'empty-resources': (MINIMAL + 'entry_resources:\n', {}),
    'pinned-octets': (EXITS3, {'state': {'mgmt-ips.txt': '# c\nus 2\nold 3\nnl 7\n'}}),
    'excluded-exit': (EXITS3, {'state': {'excluded-exits.txt': '# c\nde-2\n\n', 'mgmt-ips.txt': 'nl 2\nde-2 3\nus 4\n'}}),
    'legacy-state': (EXITS3, {'legacy': {'mgmt-ips.txt': 'us 5\nnl 9\n', 'excluded-exits.txt': 'nl\n'}}),
    'state-over-legacy': (EXITS3, {'state': {'mgmt-ips.txt': 'us 6\n'}, 'legacy': {'mgmt-ips.txt': 'us 5\nnl 9\n'}}),
}


def compare(text, *, development=False, state=None, legacy=None):
    """(старое, расхождения до записи состояния config2env, расхождения после); старое None - конфиг отвергнут."""
    with tempfile.TemporaryDirectory() as tmp:
        repo, state_dir = layout(tmp, text, state=state, legacy=legacy)

        def new():
            try:
                return nodevars.env_by_node(config.loads(text), state=state_dir, repo_root=repo,
                                            development=development)
            except config.ConfigError as error:
                return error

        before = new()
        old = old_env(repo, state_dir, development=development)
        after = new()
    if old is None:
        return None, before, after
    return old, differences(old, before), differences(old, after)


@pytest.mark.parametrize('name', sorted(MATRIX))
def test_matrix_equals_config2env(name):
    text, options = MATRIX[name]
    old, before, after = compare(text, **options)
    assert old is not None, 'config2env отверг образец матрицы'
    assert before == []
    assert after == []
    assert all(isinstance(value, str) for env in old.values() for value in env.values())


def test_samples_of_repo_equal_config2env():
    checked = 0
    for group in samples.collect().values():
        for name, text in group.items():
            try:
                config.loads(text)
            except config.ConfigError:
                continue
            old, before, after = compare(text)
            if old is None:
                assert isinstance(before, config.ConfigError), f'{name}: config2env отверг, пакет принял'
                continue
            assert (name, before, after) == (name, [], [])
            checked += 1
    assert checked >= 8


def test_positive_control_extra_and_changed_key_are_caught():
    old = {'ru': {'A': '1', 'NEW_KEY': 'x'}, 'nl': {'B': '2'}}
    new = {'ru': {'A': '01'}, 'de': {}}
    assert differences(old, new) == [('de', '<узел>', False, True), ('nl', '<узел>', True, False),
                                     ('ru', 'A', '1', '01'), ('ru', 'NEW_KEY', 'x', None)]
    assert differences({'ru': {'A': '1'}}, {'ru': {'A': 1}}) == [('ru', 'A', '1', 1)]


@pytest.mark.parametrize('text, key', [
    (MINIMAL.replace('host: 198.18.0.2', 'host: 1.2.3'), 'exits[0].host'),
    (MINIMAL.replace('tag: nl', 'tag: ru'), 'exits[0].tag'),
    (MINIMAL + 'ports:\n  hy2_direct: 70000\n', 'ports.hy2_direct'),
    (MINIMAL + 'ports:\n  hy2_hop:\n    direct: 400-500\n', 'ports.hy2_hop.direct'),
    (MINIMAL + 'openvpn:\n  proto: sctp\n', 'openvpn.proto'),
    (MINIMAL + 'wireguard:\n  mtu: 100\n', 'wireguard.mtu'),
    (MINIMAL + 'alerts:\n  window: 5\n', 'alerts.window'),
    (MINIMAL + 'panel:\n  domain: http://x\n', 'panel.domain'),
    (MINIMAL + 'backup:\n  enabled: true\n', 'backup.passphrase'),
    (MINIMAL + 'youtube:\n  route: tor\n', 'youtube.route'),
    (MINIMAL + 'youtube:\n  quic: maybe\n', 'youtube.quic'),
    (MINIMAL + 'youtube:\n  window_scale: 15\n', 'youtube.window_scale'),
    (MINIMAL.replace('    password: q\n', '    password: q\n    hy2_up_mbps: 10\n'), 'exits[nl]'),
    ('entry:\n  host: 198.18.0.1\n  password: p\n', 'exits'),
])
def test_rejected_like_config2env(text, key):
    old, before, _ = compare(text)
    assert old is None, 'config2env принял образец'
    assert isinstance(before, config.ConfigError)
    assert before.key == key


def test_error_text_hides_values():
    secret = 'S3CRETVALUE'
    with pytest.raises(config.ConfigError) as caught, tempfile.TemporaryDirectory() as tmp:
        nodevars.env_by_node(config.loads(MINIMAL + f'panel:\n  domain: "{secret} x"\n'), state=Path(tmp))
    assert secret not in str(caught.value)


def test_bash_is_present_for_oracle():
    assert shutil.which('bash')
