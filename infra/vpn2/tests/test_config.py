"""Парсер config.yaml (vpn2.config) против старого разбора config2env.parse_yaml."""
import tempfile
from pathlib import Path

import pytest
import yaml

import samples
from vpn2 import config

ORACLE = samples.load_oracle()
SOURCES = samples.collect()
ALL = {name: text for group in SOURCES.values() for name, text in group.items()}

# Число образцов по источникам, измеренное при написании; сбор меньше - красный тест.
MIN_SAMPLES = {'example': 1, 'tests': 45, 'wizard': 1}
MIN_ACCEPTED = 26
TRUE_WORDS = {'true', 'yes', 'y', '1'}

# Образец, который оракул отвергает, а loads принимает -> причина одной фразой.
KNOWN_GAPS: dict[str, str] = {}


def oracle(text):
    """Результат оракула или None, если он отверг текст."""
    try:
        return ORACLE.parse_yaml(text)
    except SystemExit:
        return None


def expected(value, field_type):
    return str(value).lower() in TRUE_WORDS if field_type is bool else str(value)


def differences(name, cfg, typed):
    """Список (образец, ключ, оракул, новый) по разделам entry, exits, ports, telegram, sso."""
    out = []

    def compare(section, old, new):
        for key, value in old.items():
            if isinstance(value, dict):
                continue
            kind = type(getattr(new, key))
            got = getattr(new, key)
            if expected(value, kind) != got:
                out.append((name, f'{section}.{key}', value, got))

    compare('entry', cfg['entry'], typed.entry)
    compare('ports', cfg['ports'], typed.ports)
    compare('telegram', cfg['telegram'], typed.telegram)
    compare('sso', cfg['sso'], typed.sso)
    if len(cfg['exits']) != len(typed.exits):
        out.append((name, 'exits', len(cfg['exits']), len(typed.exits)))
    for index, (old, new) in enumerate(zip(cfg['exits'], typed.exits)):
        compare(f'exits[{index}]', old, new)
    if cfg['hy2_hop'] is not None:
        compare('ports.hy2_hop', cfg['hy2_hop'], typed.ports.hy2_hop)
    return out


def test_samples_collected_not_empty():
    for source, minimum in MIN_SAMPLES.items():
        assert len(SOURCES[source]) >= minimum, f'{source}: {len(SOURCES[source])} < {minimum}'
    accepted = [name for name, text in ALL.items() if oracle(text) is not None]
    assert len(accepted) >= MIN_ACCEPTED


def test_accepted_samples_match_oracle():
    found = []
    for name, text in sorted(ALL.items()):
        cfg = oracle(text)
        if cfg is None:
            continue
        try:
            typed = config.loads(text)
        except config.ConfigError as error:
            found.append((name, error.key, 'принят', error.reason))
            continue
        found += differences(name, cfg, typed)
    assert not found, '\n'.join('образец %s, ключ %s, оракул %r, новый %r' % row for row in found)


def test_rejected_samples_rejected():
    assert all(KNOWN_GAPS.values()), 'у каждого исключения нужна причина'
    leaked = []
    for name, text in sorted(ALL.items()):
        if oracle(text) is not None or name in KNOWN_GAPS:
            continue
        try:
            config.loads(text)
        except config.ConfigError:
            continue
        leaked.append(name)
    assert not leaked, f'оракул отверг, loads принял: {leaked}'


EXITS_PLAIN = 'entry:\n  host: 1.2.3.4\n  password: p\nexits:\n- tag: us\n  host: 5.6.7.8\n  password: q\n'
EXITS_INDENTED = 'entry:\n  host: 1.2.3.4\n  password: p\nexits:\n  - tag: us\n    host: 5.6.7.8\n    password: q\n'


@pytest.mark.parametrize('text', [EXITS_PLAIN, EXITS_INDENTED])
def test_exit_list_with_and_without_indent(text):
    cfg = config.loads(text)
    assert [(e.tag, e.host, e.password) for e in cfg.exits] == [('us', '5.6.7.8', 'q')]
    assert oracle(text)['exits'][0]['tag'] == 'us'


def test_quotes_comments_and_non_ascii():
    text = ('entry:\n  host: "1.2.3.4"   # адрес\n  password: \'it\'\'s # not\'\n'
            'telegram:\n  bot_token: пароль-ключ\n  chat_id: "42" # чат\n'
            'sso:\n  enabled: "yes"\n  label: Вход через «Authentik»\n')
    cfg = config.loads(text)
    assert (cfg.entry.host, cfg.entry.password) == ('1.2.3.4', "it's # not")
    assert (cfg.telegram.bot_token, cfg.telegram.chat_id) == ('пароль-ключ', '42')
    assert cfg.sso.enabled is True
    assert cfg.sso.label == 'Вход через «Authentik»'
    assert differences('forms', oracle(text), cfg) == []


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
    assert cfg.entry == config.Entry(host='h', password='p', public_host='vpn.test')
    assert cfg.exits == (config.Exit(tag='t', host='e', password='w', hy2_direct_port='4443',
                                     hy2_up_mbps='100', hy2_down_mbps='200', hy2_insecure=True),)
    hop = config.Hy2Hop(enabled=True, direct='1-2', warp='3-4', mgmt='5-6', interval='30')
    assert cfg.ports == config.Ports(hy2_direct='443', hy2_warp='8443', hy2_mgmt='8444',
                                     wireguard='51820', openvpn='1194', hy2_hop=hop)
    assert cfg.telegram == config.Telegram(bot_token='b', chat_id='c', admin_username='a')
    assert cfg.sso == config.Sso(
        enabled=True, base_url='u', app_slug='s', client_id='i', client_secret='x',
        allowed_subs='z', auto_redirect=True, password_login=False, label='l', discovery_url='d')
    assert cfg.raw['ports']['hy2_hop']['interval'] == 30
    first = cfg.exits[0]
    assert (first.hy2_direct_port, first.hy2_up_mbps, first.hy2_down_mbps) == ('4443', '100', '200')
    assert first.hy2_insecure is True
    assert cfg.telegram.admin_username == 'a'
    assert (cfg.sso.base_url, cfg.sso.app_slug, cfg.sso.discovery_url) == ('u', 's', 'd')
    assert (cfg.sso.auto_redirect, cfg.sso.password_login) == (True, False)
    assert differences('all', oracle(text), cfg) == []


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


def test_positive_control_corrupted_key_is_caught():
    """Испорченный ключ в копии образца: сравнение с оракулом это должно ловить."""
    text = SOURCES['example']['example']
    broken = text.replace('host: 5.6.7.8', 'host: 5.6.7.9', 1)
    assert broken != text
    cfg = oracle(text)
    assert differences('example', cfg, config.loads(text)) == []
    rows = differences('example-broken', cfg, config.loads(broken))
    assert rows == [('example-broken', 'exits[0].host', '5.6.7.8', '5.6.7.9')]
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / 'config.yaml'
        copy.write_text(broken, encoding='utf-8')
        assert differences('copy', cfg, config.load(str(copy)))
    assert yaml.safe_load(broken)['exits'][0]['host'] == '5.6.7.9'
