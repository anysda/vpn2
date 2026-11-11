import json
import os
import sys
from pathlib import Path

import pytest
from vpn2.watchdog import failover, yt

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import snapshot
from snapshot import collect, diff

REPO = Path(__file__).resolve().parents[3]

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
    assert 'rescue.service' not in units, 'загружен и не запущен - то же, что не загружен'


def _units(root, files, loaded, show=''):
    return collect(root, fake({'systemctl list-unit-files': (0, files),
                                 'systemctl list-units': (0, loaded),
                                 'systemctl show': (0, show)}))['units']


def _transient(root, service, state, timer=True, kind='simple', sub='x', restart='no'):
    names = [service] + ([service.replace('.service', '.timer')] if timer else [])
    rows = ''.join(f'{n}  loaded  {state if n == service else "active"}  {sub}  d\n' for n in names)
    show = f'Id={service}\nType={kind}\nRestart={restart}\n\nId=ssh.service\nType=notify\nRestart=on-failure\n'
    return _units(root, '', rows, show).get(service, {}).get('active')


def test_timer_service_passing_states_are_not_written(root):
    assert _transient(root, 'sync.service', 'activating') is None
    assert _transient(root, 'sync.service', 'deactivating') is None
    assert _transient(root, 'sync.service', 'active', sub='running') is None


def test_oneshot_service_without_timer_activating_is_not_written(root):
    assert _transient(root, 'job.service', 'activating', timer=False, kind='oneshot') is None


def test_oneshot_that_remains_after_exit_is_active(root):
    assert _transient(root, 'wg-quick@wg0.service', 'active', timer=False, kind='oneshot',
                      sub='exited') == 'active'


@pytest.mark.parametrize('kind,timer', [('simple', True), ('oneshot', False), ('idle', False)])
def test_run_once_service_failed_stays_failed(root, kind, timer):
    assert _transient(root, 'sync.service', 'failed', timer=timer, kind=kind, sub='failed') == 'failed'


def test_plain_service_activating_is_kept(root):
    assert _transient(root, 'daemon.service', 'activating', timer=False) == 'activating'


def test_idle_service_that_restarts_is_a_daemon(root):
    """getty тоже Type=idle, но Restart=always: её active - удерживаемое состояние."""
    for sub in ('start-pre', 'running'):
        assert _transient(root, 'console-getty.service', 'active', timer=False, kind='idle',
                          sub=sub, restart='always') == 'active'


# Разовые службы Ubuntu: (служба, Type, есть таймер, строка list-units на ходу).
# dmesg - Type=idle: её фазы запуска systemd показывает как active (замер на 26.04).
RUN_ONCE = [
    ('dmesg.service', 'idle', False, 'active   start-pre'),
    ('dmesg.service', 'idle', False, 'active   running'),
    ('dmesg.service', 'idle', False, 'active   start-post'),
    ('dmesg.service', 'idle', False, 'deactivating stop-sigterm'),
    ('apt-news.service', 'oneshot', False, 'activating start'),
    ('esm-cache.service', 'oneshot', False, 'activating start'),
    ('e2scrub_reap.service', 'oneshot', False, 'activating start'),
    ('e2scrub_all.service', 'oneshot', True, 'activating start'),
    ('apt-daily.service', 'oneshot', True, 'activating start'),
    ('apt-daily-upgrade.service', 'oneshot', True, 'activating start-pre'),
    ('fstrim.service', 'oneshot', True, 'activating start'),
    ('systemd-tmpfiles-clean.service', 'oneshot', True, 'activating start'),
    ('man-db.service', 'oneshot', True, 'activating start'),
    ('motd-news.service', 'oneshot', True, 'deactivating stop'),
    ('fwupd-refresh.service', 'oneshot', True, 'activating start'),
    ('logrotate.service', 'simple', True, 'active   running'),
]


def _run_once_units(root, service, kind, timer, row):
    """Юниты узла: служба на ходу (row), отработала (dead) или выгружена (None)."""
    files = f'ssh.service enabled enabled\n{service} enabled enabled\n'
    loaded = 'ssh.service loaded active running d\n'
    if timer:
        name = service.replace('.service', '.timer')
        files += f'{name} enabled enabled\n'
        loaded += f'{name} loaded active waiting d\n'
    if row:
        loaded += f'{service} loaded {row} d\n'
    show = f'Id={service}\nType={kind}\nRestart=no\n\nId=ssh.service\nType=notify\nRestart=on-failure\n'
    return _units(root, files, loaded, show if row and 'dead' not in row else '')


@pytest.mark.parametrize('service,kind,timer,row', RUN_ONCE)
def test_run_once_unit_is_the_same_running_finished_and_unloaded(root, service, kind, timer, row):
    running = _run_once_units(root, service, kind, timer, row)
    finished = _run_once_units(root, service, kind, timer, 'inactive dead')
    unloaded = _run_once_units(root, service, kind, timer, None)
    assert running == finished == unloaded
    assert running[service] == {'enabled': 'enabled'}
    assert running == _run_once_units(root, service, kind, timer, row), 'два слепка подряд равны'


BOOT_FILES = """\
ssh.service        enabled  enabled
dmesg.service      enabled  enabled
e2scrub_reap.service enabled enabled
apt-news.service   static   -
cloud-final.service enabled enabled
systemd-fsckd.service static -
"""
BOOT_SHOW = ('Id=dmesg.service\nType=idle\nRestart=no\n\nId=e2scrub_reap.service\nType=oneshot\nRestart=no\n\n'
             'Id=apt-news.service\nType=oneshot\nRestart=no\n\nId=cloud-final.service\nType=oneshot\nRestart=no\n\n'
             'Id=ssh.service\nType=notify\nRestart=on-failure\n')
BOOTING = """\
ssh.service          loaded active     running   d
dmesg.service        loaded active     start-pre d
e2scrub_reap.service loaded activating start     d
apt-news.service     loaded activating start     d
cloud-final.service  loaded active     exited    d
firewalld.service    not-found inactive dead     d
"""
SETTLED = """\
ssh.service          loaded active     running   d
dmesg.service        loaded inactive   dead      d
cloud-final.service  loaded active     exited    d
"""


def test_baseline_right_after_boot_leaves_no_system_units(root):
    """База снята, пока загрузка доигрывает разовые службы; после установки они отработали."""
    base = _units(root, BOOT_FILES, BOOTING, BOOT_SHOW)
    assert base['dmesg.service'] == {'enabled': 'enabled'}
    assert 'firewalld.service' not in base
    after = _units(root, BOOT_FILES, SETTLED, BOOT_SHOW)
    assert snapshot.subtract({'units': after}, {'units': base}) == {}


def test_new_enabled_unit_is_still_reported_as_extra(root):
    base = _units(root, BOOT_FILES, BOOTING, BOOT_SHOW)
    files = BOOT_FILES + 'intruder.service enabled enabled\nonce.service enabled enabled\n'
    loaded = SETTLED + 'intruder.service loaded active running d\nonce.service loaded inactive dead d\n'
    show = BOOT_SHOW + '\nId=intruder.service\nType=simple\nRestart=always\n'
    left = snapshot.subtract({'units': _units(root, files, loaded, show)}, {'units': base})
    assert left == {'units': {'intruder.service': {'enabled': 'enabled', 'active': 'active'},
                              'once.service': {'enabled': 'enabled'}}}
    assert diff({'units': {}}, left) == ['units intruder.service: лишнее в текущем',
                                         'units once.service: лишнее в текущем']


def test_stopped_daemon_is_named(root):
    up = _units(root, 'xray.service enabled enabled\n', 'xray.service loaded active running d\n')
    down = _units(root, 'xray.service enabled enabled\n', 'xray.service loaded inactive dead d\n')
    gone = _units(root, 'xray.service enabled enabled\n', '')
    assert down == gone == {'xray.service': {'enabled': 'enabled'}}
    assert diff({'units': up}, {'units': down}) == ['units xray.service active: "active" -> нет']


def test_stand_rules_do_not_hide_run_once_units_by_name():
    """Разовые службы закрыты сборщиком по природе юнита, списком имён их не прячут."""
    rules = snapshot.load_ignore(REPO / 'infra/tests/fixtures/snapshot/ignore.yml')
    hidden = {rule['name'] for rule in rules if rule['section'] == 'units'}
    assert not hidden & {service for service, *_ in RUN_ONCE}


def test_ports(root):
    ports = collect(root, fake())['ports']
    assert ports['tcp 127.0.0.1:24283 python'] == {'process': 'python'}
    assert ports['tcp 0.0.0.0:443 anyd,nginx'] == {'process': 'anyd,nginx'}
    assert ports['udp 127.0.0.53%lo:53'] == {'process': ''}
    assert ports['tcp [::]:22'] == {'process': ''}


@pytest.mark.parametrize('port, hidden', [
    (32768, True), (39999, True), (40000, True), (59999, True), (60871, True), (60999, True),
    (123, False), (31999, False), (61000, False), (6087, False),
])
def test_ports_timesyncd_random_port_is_ignored_by_stand_rules(root, port, hidden):
    """Сокет опроса NTP живёт секунды: в слепок попадает случайно, порт каждый раз новый."""
    rules = snapshot.load_ignore(REPO / 'infra/tests/fixtures/snapshot/ignore.yml')
    line = f'udp UNCONN 0 0 0.0.0.0:{port} 0.0.0.0:* users:(("systemd-timesyn",pid=5,fd=6))\n'
    quiet, polling = collect(root, fake({'ss': (0, '')})), collect(root, fake({'ss': (0, line)}))
    assert polling['ports'] == {f'udp 0.0.0.0:{port} systemd-timesyn': {'process': 'systemd-timesyn'}}
    assert bool(diff(quiet, polling)) and bool(diff(polling, quiet))
    assert (diff(quiet, polling, rules) == []) is hidden
    assert (diff(polling, quiet, rules) == []) is hidden
    other = collect(root, fake({'ss': (0, line.replace('systemd-timesyn', 'python3'))}))
    assert diff(quiet, other, rules)


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


# Состояние, которое сторожа переписывают по таймеру: имена берутся из их кода.
WATCHDOG_STATE = (str(yt.STATE), '/var/lib/anysda-warp-watchdog/state.json',
                  failover.LANE_STATE_FILE, failover.RESTART_STATE_FILE)
STAND_RULES = REPO / 'infra/tests/fixtures/snapshot/ignore.yml'


@pytest.fixture
def installed(root):
    """Узел после установки: состояние сторожей и конфиг рядом с ним."""
    for name in WATCHDOG_STATE:
        path = root / name.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"checked_at": 1}\n')
        path.chmod(0o644)
    (root / 'var/lib/anysda-yt-failover/exits.conf').write_text('exit1\n')
    return root


@pytest.mark.parametrize('name', WATCHDOG_STATE)
def test_timer_rewrite_of_watchdog_state_is_hidden_by_stand_rules(installed, name):
    rules = snapshot.load_ignore(STAND_RULES)
    before = collect(installed, fake())
    (installed / name.lstrip('/')).write_text('{"checked_at": 2}\n')
    after = collect(installed, fake())
    assert diff(before, after) == [f'files {name} sha256: ' + diff(before, after)[0].split('sha256: ')[1]]
    assert diff(before, after, rules) == []


@pytest.mark.parametrize('name', WATCHDOG_STATE[:3])
def test_watchdog_state_wrong_mode_is_still_named(installed, name):
    before = collect(installed, fake())
    (installed / name.lstrip('/')).chmod(0o666)
    lines = diff(before, collect(installed, fake()), snapshot.load_ignore(STAND_RULES))
    assert lines == [f'files {name} mode: "0644" -> "0666"']


def test_config_change_is_caught_next_to_rewritten_state(installed):
    before = collect(installed, fake())
    for name in WATCHDOG_STATE:
        (installed / name.lstrip('/')).write_text('{"checked_at": 3}\n')
    (installed / 'etc/plain.conf').write_text('changed\n')
    (installed / 'var/lib/anysda-yt-failover/exits.conf').write_text('exit2\n')
    lines = diff(before, collect(installed, fake()), snapshot.load_ignore(STAND_RULES))
    assert [line.split(' sha256: ')[0] for line in lines] == [
        'files /etc/plain.conf', 'files /var/lib/anysda-yt-failover/exits.conf']
