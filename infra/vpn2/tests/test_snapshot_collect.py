import json
import os

import pytest

from vpn2 import snapshot
from vpn2.snapshot import collect, diff

UNIT_FILES = """\
-.mount                                      generated       -
boot.mount                                   generated       -
ssh.service                                  enabled         enabled
getty@.service                               enabled         enabled
dev-mqueue.mount                             static          -
"""

UNITS = """\
-.mount                  loaded    active   mounted   Root Mount
boot.mount               loaded    active   mounted   /boot
ssh.service              loaded    active   running   OpenBSD Secure Shell server
rescue.service           loaded    inactive dead      Rescue Shell
"""

SS = """\
udp UNCONN 0      0         127.0.0.54:53    0.0.0.0:*
udp UNCONN 0      0      127.0.0.53%lo:53    0.0.0.0:*
tcp LISTEN 0      128        127.0.0.1:24283 0.0.0.0:*     users:(("python",pid=3366,fd=7))
tcp LISTEN 0      511          0.0.0.0:443   0.0.0.0:*     users:(("nginx",pid=1,fd=5),("nginx",pid=2,fd=5),("anyd",pid=9,fd=3))
tcp LISTEN 0      4096            [::]:22       [::]:*
"""

NFT = """\
table inet filter {
\tset blocked {
\t\ttype ipv4_addr
\t\telements = { 192.0.2.1 }
\t}

\tchain input {
\t\ttype filter hook input priority filter; policy drop;
\t\tct state established,related accept
\t\ttcp dport 22 counter packets 120 bytes 7200 accept
\t}

\tchain forward {
\t\ttype filter hook forward priority filter; policy accept;
\t}
}
table ip nat {
\tchain postrouting {
\t\ttype nat hook postrouting priority srcnat; policy accept;
\t\toifname "eth0" counter packets 5 bytes 300 masquerade
\t}
}
"""

UFW = "Status: active\n\nLogging: on (low)\nDefault: deny (incoming)\n\n22/tcp  ALLOW IN  Anywhere\n"

PRIVATE = 'PRIVATEKEYVALUE0123456789abcdefghijklmnopqr='
PEER_PUB = 'PEERPUBLICKEY0123456789abcdefghijklmnopqrstu='
PRESHARED = 'PRESHAREDKEY0123456789abcdefghijklmnopqrstu='
OWN_PUB = 'OWNPUBLICKEY0123456789abcdefghijklmnopqrstuv='
WG_DUMP = (
    f'wg0\t{PRIVATE}\t{OWN_PUB}\t51820\toff\n'
    f'wg0\t{PEER_PUB}\t{PRESHARED}\t203.0.113.5:4000\t10.8.0.3/32,10.8.0.9/32\t1700000000\t10\t20\toff\n'
    f'wg0\tPEER2PUBLICKEY=\t(none)\t(none)\t10.8.0.2/32\t0\t0\t0\toff\n'
)

INSPECT = [{
    'Name': '/xray',
    'Image': 'sha256:abc',
    'Config': {'Image': 'ghcr.io/x/xray:1.8', 'Env': [
        'DB_PASSWORD=hunter2', 'api_token=tok123', 'SSH_Key=k', 'EMPTY=']},
    'State': {'Status': 'running'},
    'HostConfig': {'RestartPolicy': {'Name': 'always'}, 'NetworkMode': 'host'},
    'NetworkSettings': {'Ports': {
        '443/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '443'}], '80/tcp': None}},
    'Mounts': [
        {'Source': '/etc/xray', 'Destination': '/etc/xray', 'RW': False},
        {'Source': '/var/lib/xray', 'Destination': '/data', 'RW': True}],
}]

REPORT = {'host': 'entry', 'code': 1, 'warnings': [], 'time': 'now', 'checks': [
    {'check': 'порт 443', 'component': 'entry', 'result': 'Успех', 'reason': '', 'time': 'x'},
    {'check': 'xray', 'component': 'exit-1', 'result': 'Ошибка', 'reason': 'нет', 'time': 'x'}]}


def fake(overrides=None):
    answers = {
        'systemctl list-unit-files': (0, UNIT_FILES),
        'systemctl list-units': (0, UNITS),
        'systemctl show': (0, ''),
        'ss': (0, SS),
        'nft': (0, NFT),
        'ufw': (0, UFW),
        'docker ps': (0, 'xray\n'),
        'docker inspect': (0, json.dumps(INSPECT)),
        'wg': (0, WG_DUMP),
    }
    answers.update(overrides or {})

    def run(argv):
        for prefix in sorted(answers, key=len, reverse=True):
            if ' '.join(argv).startswith(prefix):
                return answers[prefix]
        return 127, ''
    return run


@pytest.fixture
def root(tmp_path):
    base = tmp_path / 'root'
    (base / 'etc' / 'sub' / '__pycache__').mkdir(parents=True)
    (base / 'etc' / 'sub' / '__pycache__' / 'x.pyc').write_bytes(b'cache')
    (base / 'etc' / 'plain.conf').write_text('hello\n')
    (base / 'etc' / 'plain.conf').chmod(0o644)
    (base / 'etc' / 'secret.key').write_text('s3cret\n')
    (base / 'etc' / 'secret.key').chmod(0o600)
    (base / 'etc' / 'link').symlink_to('plain.conf')
    (base / 'opt' / 'anysda-vpn2').mkdir(parents=True)
    (base / 'opt' / 'anysda-vpn2' / 'run.sh').write_text('#!/bin/sh\n')
    (base / 'opt' / 'other').mkdir()
    (base / 'opt' / 'other' / 'ignored').write_text('x')
    (base / 'var' / 'lib' / 'vpn2-emergency').mkdir(parents=True)
    (base / 'var' / 'lib' / 'vpn2-emergency' / 'state').write_text('1')
    (base / 'var' / 'lib' / 'anysda').mkdir()
    (base / 'var' / 'lib' / 'anysda' / 'verify-report.json').write_text(json.dumps(REPORT))
    return base


def test_schema_and_all_sections(root):
    result = collect(root, fake())
    assert result['schema'] == 1
    assert set(result) == {'schema', 'files', 'units', 'ports', 'firewall', 'containers', 'wg',
                           'verify'}


def test_files_records(root):
    files = collect(root, fake())['files']
    assert files['/etc/plain.conf']['type'] == 'file'
    assert files['/etc/plain.conf']['mode'] == '0644'
    assert len(files['/etc/plain.conf']['sha256']) == 64
    assert files['/etc/sub']['type'] == 'dir'
    assert files['/etc/link'] == {**files['/etc/link'], 'type': 'link', 'target': 'plain.conf'}
    assert files['/etc/secret.key']['mode'] == '0600'
    assert '/etc' in files
    assert '/opt/anysda-vpn2/run.sh' in files
    assert '/var/lib/vpn2-emergency/state' in files
    assert not any('__pycache__' in name for name in files)
    assert not any(name.startswith('/opt/other') for name in files)
    assert not any(str(root) in name for name in files)


def test_files_owner_is_name_or_number(root):
    owner = collect(root, fake())['files']['/etc/plain.conf']['owner']
    user, _, group = owner.partition(':')
    assert user and group


def test_files_unreadable_gives_error(root):
    path = root / 'etc' / 'secret.key'
    path.chmod(0)
    try:
        record = collect(root, fake())['files']['/etc/secret.key']
    finally:
        path.chmod(0o600)
    if os.geteuid() == 0:
        assert 'sha256' in record
    else:
        assert 'sha256' not in record and record['error']


def test_files_do_not_follow_links(root, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'f').write_text('x')
    (root / 'etc' / 'dirlink').symlink_to(outside)
    files = collect(root, fake())['files']
    assert files['/etc/dirlink']['type'] == 'link'
    assert not any(name.startswith('/etc/dirlink/') for name in files)


def test_collect_twice_gives_empty_diff(root):
    assert diff(collect(root, fake()), collect(root, fake())) == []


def test_changed_byte_is_named(root):
    before = collect(root, fake())
    (root / 'etc' / 'plain.conf').write_text('hellp\n')
    lines = diff(before, collect(root, fake()))
    assert len(lines) == 1
    assert lines[0].startswith('files /etc/plain.conf sha256: ')


def test_changed_chmod_is_named(root):
    before = collect(root, fake())
    (root / 'etc' / 'plain.conf').chmod(0o600)
    lines = diff(before, collect(root, fake()))
    assert lines == ['files /etc/plain.conf mode: "0644" -> "0600"']


def test_units_merge_both_lists(root):
    units = collect(root, fake())['units']
    assert units['ssh.service'] == {'enabled': 'enabled', 'active': 'active'}
    assert units['boot.mount'] == {'enabled': 'generated', 'active': 'active'}
    assert units['getty@.service'] == {'enabled': 'enabled'}
    assert units['rescue.service'] == {'active': 'inactive'}


def _transient(root, service, state, timer=True, kind='simple'):
    names = [service] + ([service.replace('.service', '.timer')] if timer else [])
    rows = ''.join(f'{n}  loaded  {state if n == service else "active"}  x  d\n' for n in names)
    show = f'Id={service}\nType={kind}\n\nId=ssh.service\nType=notify\n'
    return collect(root, fake({'systemctl list-units': (0, rows),
                                 'systemctl show': (0, show)}))['units'][service]['active']


def test_timer_service_transient_states_are_inactive(root):
    assert _transient(root, 'sync.service', 'activating') == 'inactive'
    assert _transient(root, 'sync.service', 'deactivating') == 'inactive'


def test_oneshot_service_without_timer_activating_is_inactive(root):
    assert _transient(root, 'job.service', 'activating', timer=False, kind='oneshot') == 'inactive'


def test_timer_service_failed_stays_failed(root):
    assert _transient(root, 'sync.service', 'failed') == 'failed'


def test_plain_service_activating_is_kept(root):
    assert _transient(root, 'daemon.service', 'activating', timer=False) == 'activating'


def test_ports(root):
    ports = collect(root, fake())['ports']
    assert ports['tcp 127.0.0.1:24283 python'] == {'process': 'python'}
    assert ports['tcp 0.0.0.0:443 anyd,nginx'] == {'process': 'anyd,nginx'}
    assert ports['udp 127.0.0.53%lo:53'] == {'process': ''}
    assert ports['tcp [::]:22'] == {'process': ''}


def test_ports_random_singbox_port_is_ignorable(root):
    def ss(singbox, wg='udp UNCONN 0 0 0.0.0.0:51820 0.0.0.0:*',
           py='udp UNCONN 0 0 0.0.0.0:51821 0.0.0.0:* users:(("python3",pid=7,fd=3))'):
        line = f'udp UNCONN 0 0 127.0.0.1:{singbox} 0.0.0.0:* users:(("sing-box",pid=5,fd=9))'
        return {'ss': (0, '\n'.join([line, wg, py]) + '\n')}

    first = collect(root, fake(ss(40385)))
    second = collect(root, fake(ss(35677)))
    assert first['ports']['udp 127.0.0.1:40385 sing-box'] == {'process': 'sing-box'}
    assert first['ports']['udp 0.0.0.0:51820'] == {'process': ''}
    rule = {'section': 'ports', 'name': 'udp *:[3-6][0-9][0-9][0-9][0-9] sing-box',
            'field': '*', 'reason': 'random kernel port'}
    assert diff(first, second, [rule]) == []
    lines = diff(first, second)
    assert any('udp 127.0.0.1:40385 sing-box' in line for line in lines)
    assert any('udp 127.0.0.1:35677 sing-box' in line for line in lines)
    moved_py = 'udp UNCONN 0 0 0.0.0.0:51831 0.0.0.0:* users:(("python3",pid=7,fd=3))'
    for changed in (ss(40385, wg='udp UNCONN 0 0 0.0.0.0:51830 0.0.0.0:*'), ss(40385, py=moved_py)):
        assert diff(first, collect(root, fake(changed)), [rule])


def test_firewall_nft_and_ufw(root):
    firewall = collect(root, fake())['firewall']
    assert firewall['nft inet filter input']['rules'] == [
        'type filter hook input priority filter; policy drop;',
        'ct state established,related accept',
        'tcp dport 22 counter packets 0 bytes 0 accept']
    assert firewall['nft inet filter forward']['rules'] == [
        'type filter hook forward priority filter; policy accept;']
    assert firewall['nft ip nat postrouting']['rules'][1] == (
        'oifname "eth0" counter packets 0 bytes 0 masquerade')
    assert firewall['ufw']['status'][0] == 'Status: active'
    assert '' not in firewall['ufw']['status']


def test_containers_secrets_hidden(root):
    record = collect(root, fake())['containers']['xray']
    assert record['image'] == 'ghcr.io/x/xray:1.8'
    assert record['image_id'] == 'sha256:abc'
    assert record['state'] == 'running'
    assert record['restart'] == 'always'
    assert record['network'] == 'host'
    assert record['ports'] == ['0.0.0.0:443->443/tcp']
    assert record['mounts'] == ['/etc/xray:/etc/xray:ro', '/var/lib/xray:/data:rw']
    assert record['env'] == ['DB_PASSWORD=<скрыто>', 'EMPTY=',
                             'SSH_Key=<скрыто>', 'api_token=<скрыто>']
    dumped = json.dumps(collect(root, fake()))
    assert 'hunter2' not in dumped and 'tok123' not in dumped


def test_wg_has_no_keys(root):
    result = collect(root, fake())
    assert result['wg'] == {'wg0': {'listen_port': '51820',
                                    'peers': ['10.8.0.2/32', '10.8.0.3/32,10.8.0.9/32']}}
    dumped = json.dumps(result)
    for secret in (PRIVATE, PEER_PUB, PRESHARED, OWN_PUB, 'PEER2PUBLICKEY'):
        assert secret not in dumped


def test_verify_rows(root):
    assert collect(root, fake())['verify'] == {
        'entry порт 443': {'result': 'Успех'}, 'exit-1 xray': {'result': 'Ошибка'}}


def test_verify_missing_is_empty(root):
    (root / 'var' / 'lib' / 'anysda' / 'verify-report.json').unlink()
    assert collect(root, fake())['verify'] == {}


def test_verify_broken_is_error(root):
    (root / 'var' / 'lib' / 'anysda' / 'verify-report.json').write_text('{')
    assert '_error' in collect(root, fake())['verify']


@pytest.mark.parametrize('prefix, section', [
    ('systemctl list-units', 'units'), ('systemctl list-unit-files', 'units'),
    ('systemctl show', 'units'),
    ('ss', 'ports'), ('nft', 'firewall'), ('ufw', 'firewall'),
    ('docker ps', 'containers'), ('docker inspect', 'containers'), ('wg', 'wg')])
def test_failed_command_gives_error_record(root, prefix, section):
    result = collect(root, fake({prefix: (1, '')}))
    reason = result[section]['_error']['reason']
    assert reason.endswith(': код 1')
    assert reason.split()[0] in prefix


@pytest.mark.parametrize('prefix, section', [
    ('docker ps', 'containers'), ('wg', 'wg'), ('nft', 'firewall'), ('ufw', 'firewall')])
def test_not_installed_gives_empty_section(root, prefix, section):
    answers = {prefix: (127, '')}
    if section == 'firewall':
        answers = {'nft': (127, ''), 'ufw': (127, '')}
    assert collect(root, fake(answers))[section] == {}


def test_one_firewall_tool_missing_keeps_other(root):
    firewall = collect(root, fake({'nft': (127, '')}))['firewall']
    assert list(firewall) == ['ufw']


def test_real_collect_runs_on_this_machine():
    result = collect()
    assert set(result) == {'schema', 'files', 'units', 'ports', 'firewall', 'containers', 'wg',
                           'verify'}


def test_default_run_missing_command_is_127():
    assert snapshot._run(['definitely-not-a-command-748']) == (127, '')


def test_usr_local_bin_and_sbin_are_collected(root):
    (root / 'usr' / 'local' / 'bin').mkdir(parents=True)
    (root / 'usr' / 'local' / 'bin' / 'tool').write_text('#!/bin/sh\n')
    (root / 'usr' / 'local' / 'share' / 'x').mkdir(parents=True)
    files = collect(root, fake())['files']
    assert '/usr/local/bin/tool' in files
    assert not [name for name in files if name.startswith('/usr/local/share')]


def test_changed_usr_local_bin_is_named(root):
    (root / 'usr' / 'local' / 'bin').mkdir(parents=True)
    (root / 'usr' / 'local' / 'bin' / 'tool').write_text('#!/bin/sh\n')
    before = collect(root, fake())
    with open(root / 'usr' / 'local' / 'bin' / 'tool', 'a') as handle:
        handle.write('x')
    lines = diff(before, collect(root, fake()))
    assert any(line.startswith('files /usr/local/bin/tool sha256: ') for line in lines)


def test_missing_usr_local_sbin_does_not_fail(root):
    assert not (root / 'usr' / 'local' / 'sbin').exists()
    assert 'files' in collect(root, fake())


def test_base_image_env_is_dropped(root):
    item = dict(INSPECT[0], Config={'Image': 'x', 'Env': [
        'PATH=/usr/bin', 'PYTHON_VERSION=3.14.0', 'PYTHON_SHA256=ab', 'GPG_KEY=x',
        'NODE_VERSION=22', 'LANG=C.UTF-8', 'TZ=UTC', 'PANEL_SECRET=s', 'PYTHONUNBUFFERED=1']})
    record = collect(root, fake({'docker inspect': (0, json.dumps([item]))}))['containers']['xray']
    assert record['env'] == ['PANEL_SECRET=<скрыто>', 'PYTHONUNBUFFERED=1', 'TZ=UTC']
