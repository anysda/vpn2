"""Шаги роли router: эталоны сняты с bash-стадии 20-ru-router до её удаления."""

import os

import pytest

from vpn2.router import stage

FOREIGN = '\n'.join((
    'NL_DIRECT=aaaaaaaaaaaaaaaaaaaa', 'NL_WARP=bbbbbbbbbbbbbbbbbbbb', 'NL_MGMT=cccccccccccccccccccc',
    'NL_OBFS=dddddddddddddddddddd', 'NL_CLASH=eeeeeeeeeeeeeeeeeeee', 'DE_1_DIRECT=ffffffffffffffffffff',
    'DE_1_WARP=gggggggggggggggggggg', 'DE_1_MGMT=hhhhhhhhhhhhhhhhhhhh', 'DE_1_OBFS=iiiiiiiiiiiiiiiiiiii', ''))
# Окружение, которое роль передаёт шагу: незаданное приходит пустой строкой.
STAGE_ENV = {
    'EXIT_TAGS': 'nl de-1', 'HY2_DIRECT_PORT': '443', 'HY2_WARP_PORT': '8443', 'HY2_MGMT_PORT': '3443',
    'MGMT_IP': '10.99.0.1', 'HY2_HOP_ENABLED': 'true', 'HY2_HOP_DIRECT': '', 'HY2_HOP_INTERVAL': '',
    'YT_ROUTE': 'zapret', 'YT_QUIC': '', 'YT_MARK': '', 'YT_DOMAINS': '',
    'DOMAIN_NL': 'nl.example.test', 'DOMAIN_DE_1': 'de.example.test',
    'MGMT_IP_NL': '10.99.0.2', 'MGMT_IP_DE_1': '10.99.0.3',
    'NL_HY2_UP_MBPS': '100', 'NL_HY2_DOWN_MBPS': '200', 'NL_HY2_DIRECT_PORT': '', 'NL_HY2_INSECURE': '',
    'DE_1_HY2_UP_MBPS': '', 'DE_1_HY2_DIRECT_PORT': '4443', 'DE_1_HY2_INSECURE': '1',
}
# Что экспортировала стадия перед генератором конфига на тех же входах.
STAGE_EXPORTS = {
    'DE_1_DIRECT': 'ffffffffffffffffffff', 'DE_1_HY2_DIRECT_PORT': '4443', 'DE_1_HY2_INSECURE': '1',
    'DE_1_HY2_MGMT_PORT': '3443', 'DE_1_MGMT': 'hhhhhhhhhhhhhhhhhhhh', 'DE_1_OBFS': 'iiiiiiiiiiiiiiiiiiii',
    'DE_1_WARP': 'gggggggggggggggggggg', 'DOMAIN_DE_1': 'de.example.test', 'DOMAIN_NL': 'nl.example.test',
    'EXIT_TAGS': 'nl de-1', 'HY2_DIRECT_PORT': '443', 'HY2_HOP_ENABLED': 'true', 'HY2_MGMT_PORT': '3443',
    'HY2_WARP_PORT': '8443', 'MGMT_IP': '10.99.0.1', 'MGMT_IP_DE_1': '10.99.0.3', 'MGMT_IP_NL': '10.99.0.2',
    'NL_DIRECT': 'aaaaaaaaaaaaaaaaaaaa', 'NL_HY2_CERT_PATH': 'CERTS/nl.pem', 'NL_HY2_DIRECT_PORT': '443',
    'NL_HY2_DOWN_MBPS': '200', 'NL_HY2_MGMT_PORT': '3443', 'NL_HY2_UP_MBPS': '100',
    'NL_MGMT': 'cccccccccccccccccccc', 'NL_OBFS': 'dddddddddddddddddddd', 'NL_REALITY_PORT': '443',
    'NL_REALITY_PUBKEY': 'pubkey', 'NL_REALITY_SHORT_ID': 'ab12', 'NL_REALITY_SNI': 'example.com',
    'NL_WARP': 'bbbbbbbbbbbbbbbbbbbb', 'WG_OUT_IFACE': 'eth9', 'YT_MARK': '256', 'YT_QUIC': 'block',
    'YT_ROUTE': 'zapret',
}
SSH_BLOCK = '''
Host nl
  HostName 127.0.0.1
  Port 22
  User root
  IdentityFile /root/.ssh/id_ed25519_mgmt
  IdentitiesOnly yes
  HostKeyAlias anysda-nl
  UserKnownHostsFile /root/.ssh/known_hosts_mgmt
  StrictHostKeyChecking accept-new
  ServerAliveInterval 15
  ProxyCommand nc -X 5 -x 127.0.0.1:10102 %h %p
'''


@pytest.fixture
def pinned(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'nl.pem').write_text('CERT\n')
    (src / 'nl.reality').write_text('PUB=pubkey\nSID=ab12\nSNI=example.com\nPORT=443\nJUNK=1\n')
    return src, tmp_path / 'certs'


def test_exit_environment_matches_stage_exports(pinned):
    src, certs = pinned
    env, changed = stage.exit_environment(STAGE_ENV, stage.parse_secrets(FOREIGN), src, certs, 'eth9')
    expected = {key: value.replace('CERTS', str(certs)) for key, value in STAGE_EXPORTS.items()}
    assert env == expected
    assert changed
    assert certs.stat().st_mode & 0o777 == 0o700
    assert (certs / 'nl.pem').stat().st_mode & 0o777 == 0o600


def test_exit_environment_repeat_changes_nothing_and_keeps_pin(pinned):
    src, certs = pinned
    foreign = stage.parse_secrets(FOREIGN)
    first, _ = stage.exit_environment(STAGE_ENV, foreign, src, certs, 'eth9')
    (src / 'nl.pem').unlink()
    again, changed = stage.exit_environment(STAGE_ENV, foreign, src, certs, 'eth9')
    assert again == first
    assert not changed


def test_missing_exit_secret_stops_stage(pinned):
    src, certs = pinned
    foreign = stage.parse_secrets(FOREIGN)
    del foreign['DE_1_OBFS']
    with pytest.raises(stage.StageError, match='секрет de-1 OBFS'):
        stage.exit_environment(STAGE_ENV, foreign, src, certs, 'eth9')


@pytest.mark.parametrize('line', ['NL_DIRECT=$(reboot)aaaaaaaaaaaaaaa', 'PATH=aaaaaaaaaaaaaaaaaaaa',
                                  'NL_DIRECT=short', 'export NL_WARP=bbbbbbbbbbbbbbbbbbbb'])
def test_foreign_line_that_is_not_a_password_stops_stage(line):
    with pytest.raises(stage.StageError, match='чужая строка'):
        stage.parse_secrets(FOREIGN + line + '\n')


def test_clash_secret_is_created_once(tmp_path):
    path = tmp_path / 'clash-secret.txt'
    value, changed = stage.clash_secret(path)
    assert changed and len(value) == 32 and value.isalnum()
    assert path.stat().st_mode & 0o777 == 0o600
    assert stage.clash_secret(path) == (value, False)


def test_rule_sets_replace_only_stale_and_checked(tmp_path):
    directory = tmp_path / 'rule-set'
    directory.mkdir()
    (directory / 'geoip-ru.srs').write_text('fresh')
    (directory / 'geosite-old.srs').write_text('old')
    os.utime(directory / 'geosite-old.srs', (0, 0))
    (tmp_path / 'geoip.db').write_text('legacy')
    fetched = []

    def fetch(name, dest):
        fetched.append(name)
        dest.write_text('bad' if name == 'geosite-bad' else 'new')

    def check(path):
        return path.read_text() != 'bad'

    assert stage.sync_rule_sets(directory, ['geoip-ru', 'geosite-old'], fetch, check)
    assert fetched == ['geosite-old']
    assert (directory / 'geosite-old.srs').read_text() == 'new'
    assert not (tmp_path / 'geoip.db').exists()
    assert not stage.sync_rule_sets(directory, ['geoip-ru', 'geosite-old'], fetch, check)
    with pytest.raises(stage.StageError, match='geosite-bad повреждён'):
        stage.sync_rule_sets(directory, ['geosite-bad'], fetch, check)
    assert not list(directory.glob('*.new'))


def test_publish_config_keeps_previous_on_failed_check(tmp_path):
    dest = tmp_path / 'config-base.json'
    assert stage.publish_config('{"a": 1}', dest, lambda _path: True)
    assert not stage.publish_config('{"a": 1}', dest, lambda _path: True)
    with pytest.raises(stage.StageError, match='невалиден'):
        stage.publish_config('{"a": 2}', dest, lambda _path: False)
    assert dest.read_text() == '{"a": 1}'
    assert dest.stat().st_mode & 0o777 == 0o600
    assert [path.name for path in tmp_path.iterdir()] == ['config-base.json']


def test_fingerprint_is_sha256_of_concatenated_inputs(tmp_path):
    (tmp_path / 'a').write_text('one\n')
    (tmp_path / 'b').write_text('two\n')
    # printf 'one\ntwo\n' | sha256sum
    assert stage.fingerprint([tmp_path / 'a', tmp_path / 'missing', tmp_path / 'b']) == (
        'c3f9c8c283a2b1f2f1896f27a01cbe3cddc0c9d93f752e4639035a0f5b36f6e8')


def test_ssh_config_matches_stage_text(tmp_path):
    keys = tmp_path / 'keys'
    keys.mkdir()
    (keys / 'nl.pub').write_text('ssh-ed25519 AAAAKEY comment here\n')
    (keys / 'de-1.pub').write_text('')
    text, hosts = stage.ssh_config_text(['nl', 'se'], STAGE_ENV, '/root/.ssh/known_hosts_mgmt')
    assert hosts == ['nl']
    assert text.split('\n', 1)[1] == SSH_BLOCK
    assert stage.known_hosts_text(['nl', 'de-1'], keys) == 'anysda-nl ssh-ed25519 AAAAKEY\n'


def test_ssh_config_include_goes_first_and_repeat_is_quiet(tmp_path, capsys):
    ssh = tmp_path / 'ssh'
    ssh.mkdir()
    (ssh / 'config').write_text('Host other\n  User me\n')
    assert stage.ssh_config(['nl'], STAGE_ENV, tmp_path, ssh)
    assert (ssh / 'config').read_text() == f'Include {ssh}/anysda-mgmt.conf\nHost other\n  User me\n'
    assert not stage.ssh_config(['nl'], STAGE_ENV, tmp_path, ssh)
    assert 'хосты: nl' in capsys.readouterr().out
