"""Парсер config.yaml (vpn2.config): формы значений, типы, отказы."""
import tempfile
from pathlib import Path

import pytest

from vpn2 import config

EXITS_PLAIN = 'entry:\n  host: 1.2.3.4\n  password: p\nexits:\n- tag: us\n  host: 5.6.7.8\n  password: q\n'
EXITS_INDENTED = 'entry:\n  host: 1.2.3.4\n  password: p\nexits:\n  - tag: us\n    host: 5.6.7.8\n    password: q\n'


@pytest.mark.parametrize('text', [EXITS_PLAIN, EXITS_INDENTED])
def test_exit_list_with_and_without_indent(text):
    cfg = config.loads(text)
    assert [(e.tag, e.host, e.password) for e in cfg.exits] == [('us', '5.6.7.8', 'q')]


def test_quotes_comments_and_non_ascii():
    text = ('entry:\n  host: "1.2.3.4"   # адрес\n  password: \'it\'\'s # not\'\n'
            'telegram:\n  bot_token: пароль-ключ\n  chat_id: "42" # чат\n'
            'sso:\n  enabled: "yes"\n  label: Вход через «Authentik»\n')
    cfg = config.loads(text)
    assert (cfg.entry.host, cfg.entry.password) == ('1.2.3.4', "it's # not")
    assert (cfg.raw['telegram']['bot_token'], cfg.raw['telegram']['chat_id']) == ('пароль-ключ', '42')
    assert cfg.raw['sso'] == {'enabled': 'yes', 'label': 'Вход через «Authentik»'}


def test_all_typed_fields_are_read():
    text = ('entry:\n  host: h\n  password: p\n  public_host: vpn.test\n'
            'exits:\n  - tag: t\n    host: e\n    password: w\n    hy2_direct_port: 4443\n'
            '    hy2_up_mbps: 100\n    hy2_down_mbps: 200\n    hy2_insecure: true\n'
            'ports:\n  hy2_direct: 443\n  hy2_warp: 8443\n  hy2_mgmt: 8444\n  wireguard: 51820\n'
            '  openvpn: 1194\n  hy2_hop:\n    enabled: true\n    direct: 1-2\n    warp: 3-4\n'
            '    mgmt: 5-6\n    interval: 30\n'
            'telegram:\n  bot_token: b\n  chat_id: c\n  admin_username: a\n'
            'sso:\n  enabled: true\n  base_url: u\n  app_slug: s\n  client_id: i\n'
            '  client_secret: x\n  allowed_subs: z\n  auto_redirect: true\n'
            '  password_login: false\n  label: l\n  discovery_url: d\n')
    cfg = config.loads(text)
    assert cfg.entry == config.Node('ru', 'h', 'p')
    assert cfg.exits == (config.Node('t', 'e', 'w'),)
    assert cfg.raw['ports']['hy2_hop']['interval'] == '30'
    assert cfg.raw['exits'][0]['hy2_insecure'] == 'true'


def test_raw_keeps_strings_of_every_section():
    text = ('entry:\n  host: h\nbackup:\n  enabled: yes\n  retention: 07\n  s3:\n    secret_key: 0123\n'
            'failover:\n  restart_min_interval: 0600\nentry_resources:\n  panel_memory_mib: 0512\n'
            'wireguard:\n  mtu: 1420\n  split_local: no\nadmin:\n  user: 1e3\nyoutube:\n  mark: 0x10\n'
            'orchestrator_key: 2026-01-01\n')
    cfg = config.loads(text)
    assert cfg.raw == {
        'entry': {'host': 'h'}, 'backup': {'enabled': 'yes', 'retention': '07', 's3': {'secret_key': '0123'}},
        'failover': {'restart_min_interval': '0600'}, 'entry_resources': {'panel_memory_mib': '0512'},
        'wireguard': {'mtu': '1420', 'split_local': 'no'}, 'admin': {'user': '1e3'}, 'youtube': {'mark': '0x10'},
        'orchestrator_key': '2026-01-01'}
    extra = config.loads(text + 'alerts:\n  cpu_pct: 90\n')
    assert extra.raw['alerts'] == {'cpu_pct': '90'}


FOREIGN = ['entry:\n  password: &a\n', 'entry:\n  password: &a x\n', 'entry:\n  password: !!str 123\n',
           'entry:\n  password: !!binary aGk=\n', 'entry:\n  password: ! p\n', 'entry: &e\n  host: h\n',
           'entry:\n  password: *a\n', '---\nentry:\n  host: h\n', 'entry:\n  host: h\n...\n',
           'entry:\n  "host": h\n', '"entry":\n  host: h\n', '\ufeffentry:\n  host: h\n', '%YAML 1.1\n---\nentry:\n  host: h\n',
           'entry:\n  host: h\nexits:\n  -\n    tag: t\n    host: e\n', 'entry:\n  password: [a\n',
           'entry:\n  password: {a: b\n']


@pytest.mark.parametrize('text', FOREIGN)
def test_yaml_beyond_old_format_is_rejected(text):
    with pytest.raises(config.ConfigError):
        config.loads(text)


@pytest.mark.parametrize('text', ['', '\n# только комментарий\n', '- a\n- b\n', 'просто строка\n',
                                  'entry: [a]\n', 'entry:\n  host: a\n  host: b\n',
                                  'entry:\n  hots: a\n', 'sso:\n  enabled: maybe\n',
                                  'entry:\n  host: "a\n'])
def test_bad_files_raise_config_error(text):
    with pytest.raises(config.ConfigError) as caught:
        config.loads(text)
    assert caught.value.key and caught.value.reason
    assert str(caught.value) == f'config.yaml: {caught.value.key}: {caught.value.reason}'


def test_error_text_hides_values():
    with pytest.raises(config.ConfigError) as caught:
        config.loads('entry:\n  password: "top-secret\n')
    assert 'top-secret' not in str(caught.value)


def test_load_reads_file():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'config.yaml'
        path.write_text(EXITS_PLAIN, encoding='utf-8')
        assert config.load(str(path)).entry.host == '1.2.3.4'

