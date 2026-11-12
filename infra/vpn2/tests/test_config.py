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
MIN_SAMPLES = {'example': 1, 'tests': 40, 'wizard': 1}
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


def old_raw(cfg):
    """Разбор оракула в форме Config.raw: непустые разделы, hy2_hop внутри ports."""
    out = {key: value for key, value in cfg.items() if key != 'hy2_hop' and value}
    if cfg['hy2_hop'] is not None:
        out['ports'] = {**cfg['ports'], 'hy2_hop': cfg['hy2_hop']}
    return out


def walk(old, new, path):
    """Пары (путь, оракул, новый) там, где деревья расходятся значением, типом или набором ключей."""
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() | new.keys()):
            yield from walk(old.get(key), new.get(key), f'{path}.{key}' if path else key)
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for index, pair in enumerate(zip(old, new)):
            yield from walk(*pair, f'{path}[{index}]')
    elif old != new or type(old) is not type(new):
        yield path, old, new


def differences(name, cfg, typed):
    """Список (образец, ключ, оракул, новый): raw по всем разделам и ключам, число экзитов."""
    out = [(name, f'raw:{path}', old, new)
           for path, old, new in walk(old_raw(cfg), {k: v for k, v in typed.raw.items() if v}, '')]
    if len(cfg['exits']) != len(typed.exits):
        out.append((name, 'exits', len(cfg['exits']), len(typed.exits)))
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
    assert (cfg.raw['telegram']['bot_token'], cfg.raw['telegram']['chat_id']) == ('пароль-ключ', '42')
    assert cfg.raw['sso'] == {'enabled': 'yes', 'label': 'Вход через «Authentik»'}
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
    assert cfg.entry == config.Node('ru', 'h', 'p')
    assert cfg.exits == (config.Node('t', 'e', 'w'),)
    assert cfg.raw['ports']['hy2_hop']['interval'] == '30'
    assert cfg.raw['exits'][0]['hy2_insecure'] == 'true'
    assert differences('all', oracle(text), cfg) == []


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
    assert differences('raw', oracle(text), cfg) == []
    assert differences('raw', {**oracle(text), 'admin': {'user': '1000'}}, cfg) == [('raw', 'raw:admin.user', '1000', '1e3')]
    extra = config.loads(text + 'alerts:\n  cpu_pct: 90\n')
    assert differences('raw', oracle(text), extra) == [('raw', 'raw:alerts', None, {'cpu_pct': '90'})]


HAND_WRITTEN = ['password: a: b', 'password: @p', 'password: %p', 'password: `p', 'password: - p', 'password: ? p',
                'password: p\t# c', 'password: p\t', 'password:\tp', 'host: 1.2.3.4 \t', 'host:h', 'password: p:',
                'password: :', 'password: =', 'password: <<', 'password: ,p', 'password: ]p', 'password: }p',
                'password: \t', 'password: \t# c', "password: it's: here", 'password: "p"\t# c']
FOREIGN = ['entry:\n  password: &a\n', 'entry:\n  password: &a x\n', 'entry:\n  password: !!str 123\n',
           'entry:\n  password: !!binary aGk=\n', 'entry:\n  password: ! p\n', 'entry: &e\n  host: h\n',
           'entry:\n  password: *a\n', '---\nentry:\n  host: h\n', 'entry:\n  host: h\n...\n',
           'entry:\n  "host": h\n', '"entry":\n  host: h\n', '\ufeffentry:\n  host: h\n', '%YAML 1.1\n---\nentry:\n  host: h\n',
           'entry:\n  host: h\nexits:\n  -\n    tag: t\n    host: e\n', 'entry:\n  password: [a\n',
           'entry:\n  password: {a: b\n']


@pytest.mark.parametrize('line', HAND_WRITTEN)
def test_hand_written_plain_values_read_as_old(line):
    text = f'entry:\n  {line}\nexits:\n  - {line}\n    tag: t\n'
    cfg = oracle(text)
    assert cfg is not None
    assert differences(line, cfg, config.loads(text)) == []


@pytest.mark.parametrize('text', FOREIGN)
def test_yaml_beyond_old_format_is_rejected(text):
    assert oracle(text) is None
    with pytest.raises(config.ConfigError):
        config.loads(text)


def test_plain_value_forms_match_oracle_by_enumeration():
    """Все значения из одного и двух знаков разметки: вердикт и значение как у оракула."""
    marks = '@%`-?,]}[{=<>&*!|#:\'" \t\\~.+/$a1'
    found = []
    for value in list(marks) + [a + b for a in marks for b in marks]:
        for template in ('entry:\n  password: {}\n', 'exits:\n  - tag: {}\n    host: e\n', 'sso:\n  enabled: {}\n',
                         'orchestrator_key: {}\n'):
            text = template.format(value)
            cfg = oracle(text)
            try:
                new = config.loads(text)
            except config.ConfigError:
                new = None
            if (cfg is None) != (new is None) or (cfg is not None and differences(value, cfg, new)):
                found.append((template.split(':')[0], value))
    assert not found, found


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
    assert rows == [('example-broken', 'raw:exits[0].host', '5.6.7.8', '5.6.7.9')]
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / 'config.yaml'
        copy.write_text(broken, encoding='utf-8')
        assert differences('copy', cfg, config.load(str(copy)))
    assert yaml.safe_load(broken)['exits'][0]['host'] == '5.6.7.9'
